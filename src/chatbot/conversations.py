"""Session-owned SQLite history. No PDF ownership or deletion lives here."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage

from .documents import DocumentError


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    def __init__(self, index_dir):
        self.path = Path(index_dir) / "study-conversations.sqlite3"
        self.lock = threading.RLock()
        self.turn_locks = {}

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS conversations "
                "(owner TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(owner, id))")
            connection.execute("CREATE TABLE IF NOT EXISTS conversation_imports "
                "(owner TEXT NOT NULL, legacy_id TEXT NOT NULL, conversation_id TEXT NOT NULL, PRIMARY KEY(owner, legacy_id))")
            with connection:
                yield connection

    def create(self, owner, document_ids=None, title="New chat"):
        now = timestamp()
        item = {"id": str(uuid.uuid4()), "title": title, "title_auto": title == "New chat",
                "created_at": now, "updated_at": now, "document_ids": list(dict.fromkeys(document_ids or [])),
                "messages": []}
        with self.lock, self.connection() as connection:
            connection.execute("INSERT INTO conversations VALUES (?, ?, ?)", (owner, item["id"], json.dumps(item)))
        return item

    def get(self, owner, conversation_id):
        with self.lock, self.connection() as connection:
            row = connection.execute("SELECT payload FROM conversations WHERE owner = ? AND id = ?",
                                     (owner, conversation_id)).fetchone()
        if row is None:
            raise DocumentError("This conversation is unavailable in this browser session.", 404, "conversation_missing")
        return json.loads(row[0])

    def list(self, owner, include_empty=False):
        with self.lock, self.connection() as connection:
            rows = connection.execute("SELECT payload FROM conversations WHERE owner = ?", (owner,)).fetchall()
        items = [json.loads(row[0]) for row in rows]
        if not include_empty:
            items = [item for item in items if item.get("messages")]
        return sorted(({key: item[key] for key in ("id", "title", "created_at", "updated_at")} for item in items),
                      key=lambda item: (item["updated_at"], item["id"]), reverse=True)

    def save(self, owner, item):
        item["updated_at"] = timestamp()
        with self.lock, self.connection() as connection:
            changed = connection.execute("UPDATE conversations SET payload = ? WHERE owner = ? AND id = ?",
                                         (json.dumps(item), owner, item["id"])).rowcount
        if changed != 1:
            raise DocumentError("This conversation was deleted. Start a new chat.", 404, "conversation_missing")
        return item

    @contextmanager
    def turn(self, owner, conversation_id):
        # One in-process worker, as with PDF jobs. Reject overlapping writes
        # instead of holding a SQLite transaction during a provider request.
        with self.lock:
            turn_lock = self.turn_locks.setdefault((owner, conversation_id), threading.Lock())
        if not turn_lock.acquire(blocking=False):
            raise DocumentError("This conversation is busy. Wait for its current request and retry.", 409, "conversation_busy")
        try:
            yield self.get(owner, conversation_id)
        finally:
            turn_lock.release()

    def append(self, owner, item, role, text, **metadata):
        message = {"id": str(uuid.uuid4()), "role": role, "text": text,
                   "time": int(time.time() * 1000), "citations": [], "source_ids": [], **metadata}
        item["messages"].append(message)
        if role == "user" and item.get("title_auto"):
            item["title"] = " ".join(text.split())[:80] or "New chat"
            item["title_auto"] = False
        self.save(owner, item)
        return message

    def update(self, owner, conversation_id, title=None, document_ids=None):
        with self.turn(owner, conversation_id) as item:
            if title is not None:
                if not title.strip():
                    raise DocumentError("Enter a conversation title.", 400)
                item.update(title=title.strip(), title_auto=False)
            if document_ids is not None:
                item["document_ids"] = list(dict.fromkeys(document_ids))
            return self.save(owner, item)

    def delete(self, owner, conversation_id):
        with self.turn(owner, conversation_id), self.lock, self.connection() as connection:
            connection.execute("DELETE FROM conversations WHERE owner = ? AND id = ?", (owner, conversation_id))
        # Import receipts intentionally survive: deleting an imported chat
        # must not cause a stale browser backup to recreate it on refresh.
        return {"deleted": True, "conversation_id": conversation_id}

    def import_legacy(self, owner, legacy_id, messages, document_ids):
        with self.lock, self.connection() as connection:
            row = connection.execute("SELECT conversation_id FROM conversation_imports WHERE owner = ? AND legacy_id = ?",
                                     (owner, legacy_id)).fetchone()
            if row:
                return {"conversation_id": row[0], "imported": False}
            now = timestamp()
            first = next((message["text"] for message in messages if message["role"] == "user"), "Imported chat")
            item = {"id": str(uuid.uuid4()), "title": " ".join(first.split())[:80] or "Imported chat",
                    "title_auto": False, "created_at": now, "updated_at": now,
                    "document_ids": document_ids, "messages": messages}
            connection.execute("INSERT INTO conversations VALUES (?, ?, ?)", (owner, item["id"], json.dumps(item)))
            connection.execute("INSERT INTO conversation_imports VALUES (?, ?, ?)", (owner, legacy_id, item["id"]))
        return {"conversation_id": item["id"], "imported": True}


@lru_cache(maxsize=8)
def _cached_store(index_dir):
    return ConversationStore(index_dir)


_stores_lock = threading.Lock()


def get_conversation_store(index_dir):
    # lru_cache can compute the first value twice on concurrent misses. Sharing
    # this lock ensures every request uses the same per-conversation turn lock.
    with _stores_lock:
        return _cached_store(str(Path(index_dir).absolute()))


def source_snapshot(library):
    """Unavailable metadata never turns archived references into ready sources."""
    try:
        available = {item["id"]: item for item in library.list_documents()}
        for item in library.deletions().values():
            available[item["id"]] = {**item, "status": "deleted"}
        return available
    except Exception:
        return None


def present_conversation(item, sources):
    # Round-trip to avoid altering the persistent history when annotating it.
    result = json.loads(json.dumps(item))
    result["document_ids"] = [key for key in item["document_ids"]
                              if sources is None or sources.get(key, {}).get("status") != "deleted"]
    for message in result["messages"]:
        message["unavailable_source_ids"] = [key for key in message.get("source_ids", [])
            if sources is None or (sources or {}).get(key, {}).get("status") != "ready"]
        for citation in message.get("citations", []):
            source = (sources or {}).get(citation.get("document_id"), {})
            changed = bool(citation.get("revision")) and citation["revision"] != source.get("revision")
            citation["available"] = not message.get("imported") and sources is not None and source.get("status") == "ready" and (
                not citation.get("revision") or citation["revision"] == source.get("revision"))
            citation["availability"] = ("unverified" if sources is None else
                source.get("status", "missing") if source.get("status") != "ready" else
                "archived" if message.get("imported") else "changed" if changed else "ready")
    return result


def context_messages(item, selected_ids, sources):
    """Rebuild real chatbot memory from SQLite, without stale PDF evidence."""
    history, pending, context_ids = [], None, []
    for message in item["messages"][-80:]:
        if message["role"] == "user":
            pending = HumanMessage(content=message["text"], id=message["id"])
        elif message["role"] == "assistant" and pending is not None:
            mode = message.get("answer_mode", "legacy")
            ids = message.get("source_ids", [])
            context_ids = ids
            revisions = message.get("source_revisions", {})
            same_sources = bool(ids) and set(ids) == set(selected_ids) and sources is not None and all(
                sources.get(key, {}).get("status") == "ready" and revisions.get(key) == sources[key].get("revision")
                for key in ids)
            usable = mode == "general" or (mode == "document" and same_sources)
            text = message["text"] if usable else "An earlier document response is unavailable for the current sources."
            history.extend([pending, AIMessage(content=text, id=message["id"],
                additional_kwargs={"answer_mode": mode if usable else "status", "source_ids": ids})])
            pending = None
        elif message["role"] == "error":
            pending = None
    return history, context_ids
