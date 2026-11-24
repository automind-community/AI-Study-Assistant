"""Real per-PDF processing, persistent state, and selected-source retrieval."""

from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = str(ROOT / "data")
DB_DIR = str(ROOT / "chroma_db")
COLLECTION = "study_documents_v2"  # Do not modify unverified legacy collections.
MODEL = "sentence-transformers/all-MiniLM-L6-v2"
log = logging.getLogger("uvicorn.error.documents")


class DocumentError(Exception):
    def __init__(self, message, status_code=409, code="document_unavailable"):
        super().__init__(message)
        self.status_code, self.code = status_code, code


class ProcessingCancelled(DocumentError):
    def __init__(self):
        super().__init__("This document was deleted. Its source is unavailable.", 410, "document_deleted")


_embeddings_instance = None
_embeddings_lock = threading.Lock()


def _resolve_local_model_target():
    """Locate downloaded model snapshot directory to avoid any HuggingFace Hub network checks."""
    try:
        hub_dir = Path.home() / ".cache" / "huggingface" / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots"
        if hub_dir.is_dir():
            for snap in hub_dir.iterdir():
                if snap.is_dir() and ((snap / "model.safetensors").exists() or (snap / "pytorch_model.bin").exists()):
                    return str(snap)
    except Exception as e:
        log.debug("Could not resolve local snapshot directory: %s", e)
    return MODEL


def embeddings():
    """Thread-safe singleton for the embedding model, prioritizing cached local files."""
    global _embeddings_instance
    if _embeddings_instance is not None:
        return _embeddings_instance
    with _embeddings_lock:
        if _embeddings_instance is not None:
            return _embeddings_instance
        from langchain_huggingface import HuggingFaceEmbeddings
        target = _resolve_local_model_target()
        is_local_dir = Path(target).is_dir()
        if is_local_dir:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        log.info("Initializing HuggingFaceEmbeddings singleton with target=%s (local_dir=%s)", target, is_local_dir)
        try:
            model_kwargs = {} if is_local_dir else {"local_files_only": True}
            _embeddings_instance = HuggingFaceEmbeddings(
                model_name=target,
                model_kwargs=model_kwargs,
                encode_kwargs={"normalize_embeddings": True, "batch_size": 64},
            )
        except Exception as error:
            log.warning("Local-only embeddings init failed (%s); attempting fallback", error, exc_info=True)
            _embeddings_instance = HuggingFaceEmbeddings(
                model_name=MODEL,
                encode_kwargs={"normalize_embeddings": True, "batch_size": 64},
            )
        return _embeddings_instance


class DocumentLibrary:
    """Local single-worker jobs. Constructing/listing a library never embeds PDFs."""

    def __init__(self, data_dir=DATA_DIR, index_dir=DB_DIR):
        self.data_dir, self.index_dir = Path(data_dir).absolute(), Path(index_dir).absolute()
        self.manifest = self.index_dir / "study-library.sqlite3"
        self.lock, self.worker = threading.RLock(), threading.Lock()
        self.pending = set()
        self.client = None

    @staticmethod
    def document_id(name):
        return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]

    def files(self):
        try:
            scan = os.scandir(self.data_dir)
        except FileNotFoundError:
            return []
        files = []
        with scan:
            for entry in scan:
                if not entry.name.lower().endswith(".pdf"):
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    files.append(dict(id=self.document_id(entry.name), filename=entry.name,
                                      size_bytes=info.st_size, modified_ns=info.st_mtime_ns))
        return sorted(files, key=lambda item: (item["filename"].casefold(), item["filename"]))

    def records(self):
        with self.lock:
            if not self.manifest.exists():
                return {}
            with closing(sqlite3.connect(self.manifest)) as connection:
                return {key: json.loads(value) for key, value in connection.execute("SELECT id, payload FROM documents")}

    def write(self, record):
        with self.lock:
            self.require_present(record["id"])
            self.index_dir.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.manifest, timeout=30)) as connection:
                with connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                    connection.execute("INSERT INTO documents VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                                       (record["id"], json.dumps(record)))

    # Document management is independent of conversation storage. Minimal
    # tombstones retain identity, never extracted content or processing results.
    def deletions(self):
        with self.lock:
            if not self.manifest.exists():
                return {}
            with closing(sqlite3.connect(self.manifest)) as connection:
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_documents'").fetchone():
                    return {}
                return {key: json.loads(value) for key, value in connection.execute("SELECT id, payload FROM deleted_documents")}

    def require_present(self, document_id):
        if document_id in self.deletions():
            raise ProcessingCancelled()

    def delete_document(self, document_id):
        """Cancel publication first; delete only this server-resolved source.

        The lock also guards processing writes/upserts and upload publication.
        Embedding work can finish, but its results cannot pass require_present.
        An incomplete cleanup stays visible and can be retried after a restart.
        """
        with self.lock:
            tombstone = self.deletions().get(document_id)
            file = next((item for item in self.files() if item["id"] == document_id), None)
            if tombstone is None and file is None:
                raise DocumentError("This PDF is missing. Refresh Sources.", 404)
            if tombstone and tombstone.get("complete"):
                return {"deleted": True, "document_id": document_id}
            filename = (tombstone or file)["filename"]
            path = self.data_dir / filename
            if self.document_id(filename) != document_id or path.parent.resolve() != self.data_dir.resolve():
                raise DocumentError("The stored document path could not be verified.", 409)
            if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)):
                raise DocumentError("Only the stored regular PDF file can be deleted.", 409)
            tombstone = tombstone or {"id": document_id, "filename": filename,
                "deleted_at": datetime.now(timezone.utc).isoformat(), "complete": False}
            self.index_dir.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.manifest, timeout=30)) as connection:
                with connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                    connection.execute("CREATE TABLE IF NOT EXISTS deleted_documents (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                    connection.execute("INSERT OR REPLACE INTO deleted_documents VALUES (?, ?)",
                                       (document_id, json.dumps(tombstone)))
            self.pending.discard(document_id)
            try:
                from chromadb.errors import NotFoundError
                try:
                    # All revisions, including partial batches and superseded
                    # chunks. Never delete a whole collection or legacy index.
                    self.collection().delete(where={"document_id": document_id})
                except NotFoundError:
                    pass  # A saved/unindexed PDF may have no collection yet.
                path.unlink(missing_ok=True)
                tombstone["complete"] = True
                with closing(sqlite3.connect(self.manifest, timeout=30)) as connection:
                    with connection:
                        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
                        connection.execute("UPDATE deleted_documents SET payload = ? WHERE id = ?",
                                           (json.dumps(tombstone), document_id))
            except Exception as error:
                log.exception("Document deletion incomplete document=%s", document_id)
                raise DocumentError("Deletion is incomplete. This PDF is unavailable; retry Delete document in Sources.",
                                    503, "deletion_incomplete") from error
            return {"deleted": True, "document_id": document_id}

    def collection(self, create=False):
        import chromadb
        with self.lock:
            if self.client is None:
                log.info("Opening verified PDF index collection=%s", COLLECTION)
                self.client = chromadb.PersistentClient(path=str(self.index_dir / "study-v2"))
            method = self.client.get_or_create_collection if create else self.client.get_collection
            # Use explicit vectors, never Chroma's implicit embedding/download path.
            return method(name=COLLECTION, embedding_function=None)

    def indexed(self, record):
        ids = record.get("chunk_ids", [])
        if not ids or record.get("embedding_model") != MODEL or record.get("chunk_count") != len(ids):
            return False
        result = self.collection().get(ids=ids, include=["metadatas"])
        metadata = result.get("metadatas") or []
        return set(result["ids"]) == set(ids) and len(metadata) == len(ids) and all(
            item and item.get("document_id") == record["id"] and item.get("revision") == record.get("revision")
            and item.get("filename") == record["filename"] and isinstance(item.get("page"), int)
            and 1 <= item["page"] <= record.get("page_count", 0)
            for item in metadata)

    @staticmethod
    def public(record):
        return {key: record.get(key) for key in (
            "id", "filename", "size_bytes", "status", "stage", "error", "warning", "page_count", "chunk_count", "revision")}

    def list_documents(self):
        with self.lock:
            records, pending, deleted = self.records(), set(self.pending), self.deletions()
        documents, files = [], self.files()
        for file in files:
            if file["id"] in deleted:
                continue
            old = records.get(file["id"], {})
            item = {**old, **file}
            item.setdefault("status", "saved")
            item.setdefault("stage", "saved")
            if old.get("status") == "ready":
                if any(old.get(key) != file[key] for key in ("size_bytes", "modified_ns")):
                    item.update(status="saved", stage="changed", error="File changed. Process this version before chatting.")
                else:
                    try:
                        verified = self.indexed(old)
                    except Exception:
                        log.exception("Index verification failed document=%s", file["id"])
                        verified = False
                    if not verified:
                        item.update(status="failed", stage="index", error="The search index is unavailable or incomplete. Retry processing.")
            elif old.get("status") == "processing" and file["id"] not in pending:
                item.update(status="failed", stage="interrupted", error="Processing was interrupted. Retry indexing.")
            documents.append(self.public(item))
        for item in deleted.values():
            if not item.get("complete"):
                size = next((file["size_bytes"] for file in files if file["id"] == item["id"]), 0)
                documents.append(self.public({**item, "size_bytes": size, "status": "failed", "stage": "deleting",
                    "error": "Deletion is incomplete. Retry Delete document; this source cannot be processed or used."}))
        return documents

    def file(self, document_id):
        self.require_present(document_id)
        item = next((item for item in self.files() if item["id"] == document_id), None)
        if item is None:
            raise DocumentError("This PDF is missing. Refresh Sources.", 404)
        return item

    @staticmethod
    def open_source(path):
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise DocumentError("Only regular PDF files can be processed.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not stat.S_ISREG(after.st_mode):
                raise DocumentError("The PDF changed while opening it. Retry processing.")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def digest(handle):
        handle.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
        handle.seek(0)
        return digest.hexdigest()

    def save_upload(self, filename, stream):
        if not isinstance(filename, str) or not filename.lower().endswith(".pdf"):
            raise DocumentError("Choose a PDF file.", 400, "invalid_upload")
        if len(filename) > 180 or any(c in filename for c in '/\\<>:"|?*') or any(ord(c) < 32 for c in filename):
            raise DocumentError("Use a PDF filename of at most 180 characters without paths or reserved characters.", 400, "invalid_upload")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.data_dir, prefix=".upload-", suffix=".tmp", delete=False) as target:
                temporary = Path(target.name)
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    target.write(block)
            with temporary.open("rb") as handle:
                digest = self.digest(handle)
            with self.lock:
                candidate, number = filename, 1
                while True:
                    destination = self.data_dir / candidate
                    # Never reuse a deleted identity. Old citations and queued
                    # callbacks must not acquire the contents of a new upload.
                    if self.document_id(candidate) in self.deletions():
                        number += 1
                        candidate = f"{Path(filename).stem} ({number}){Path(filename).suffix}"
                        continue
                    if destination.exists() or destination.is_symlink():
                        if not destination.is_symlink() and destination.is_file():
                            with self.open_source(destination) as handle:
                                if self.digest(handle) == digest:
                                    return next(item for item in self.list_documents() if item["filename"] == candidate)
                        number += 1
                        candidate = f"{Path(filename).stem} ({number}){Path(filename).suffix}"
                        continue
                    try:
                        os.link(temporary, destination)  # Atomic publication, never overwrite user files.
                        break
                    except FileExistsError:
                        continue
                record = {**self.file(self.document_id(candidate)), "status": "saved", "stage": "saved"}
                self.write(record)
                log.info("PDF saved document=%s filename=%s bytes=%s", record["id"], candidate, record["size_bytes"])
                return self.public(record)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def queue(self, document_id):
        with self.lock:
            file = self.file(document_id)
            if document_id in self.pending:
                return False
            record = {**self.records().get(document_id, {}), **file}
            record.update(status="processing", stage="queued", error=None)
            self.write(record)
            self.pending.add(document_id)
            return True

    @staticmethod
    def extract_pages(handle):
        from pypdf import PdfReader
        try:
            reader = PdfReader(handle)
            if reader.is_encrypted:
                raise DocumentError("This PDF is encrypted. Export an unlocked copy and upload it again.")
            pages = [(n, (page.extract_text() or "").strip()) for n, page in enumerate(reader.pages, 1)]
        except DocumentError:
            raise
        except Exception as error:
            raise DocumentError("This PDF could not be read. It may be empty or corrupt; upload a readable copy.") from error
        usable = [(n, text) for n, text in pages if text]
        if not usable:
            raise DocumentError("No usable text was extracted. Scanned/image-only PDFs need OCR, which is not included.")
        warning = f"{len(pages) - len(usable)} page(s) have no extractable text and were skipped. OCR is not included." if len(usable) != len(pages) else None
        return usable, len(pages), warning

    def process_document(self, document_id):
        record, stage = None, "extracting"
        try:
            with self.worker:
                file = self.file(document_id)
                previous = self.records().get(document_id, {})
                record = {**file, "status": "processing", "stage": stage, "error": None}
                self.write(record)
                log.info("PDF extraction started document=%s filename=%s", document_id, file["filename"])
                with self.open_source(self.data_dir / file["filename"]) as handle:
                    revision = self.digest(handle)
                    if previous.get("revision") == revision:
                        try:
                            reusable = self.indexed(previous)
                        except Exception:
                            reusable = False
                        if reusable:
                            record = {**previous, **file, "status": "ready", "stage": "ready", "error": None}
                            self.write(record)
                            log.info("PDF index reused document=%s chunks=%s", document_id, record["chunk_count"])
                            return
                    pages, page_count, warning = self.extract_pages(handle)
                log.info("PDF extracted document=%s pages=%s usable_pages=%s", document_id, page_count, len(pages))
                stage = "chunking"
                record.update(stage=stage)
                self.write(record)
                from langchain_text_splitters import RecursiveCharacterTextSplitter
                splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
                texts, metadata, ids = [], [], []
                for page, text in pages:
                    for number, chunk in enumerate(splitter.split_text(text)):
                        if chunk.strip():
                            ids.append(f"{document_id}:{revision}:{page}:{number}")
                            texts.append(chunk)
                            metadata.append(dict(document_id=document_id, revision=revision, filename=file["filename"], page=page))
                if not texts:
                    raise DocumentError("No usable text chunks were produced. This PDF needs a readable text layer.")
                stage = "embedding"
                record.update(stage=stage)
                self.write(record)
                log.info("PDF embedding started document=%s chunks=%s model=%s", document_id, len(texts), MODEL)
                model, collection = embeddings(), self.collection(create=True)
                for offset in range(0, len(texts), 64):
                    self.require_present(document_id)
                    batch = texts[offset:offset + 64]
                    vectors = model.embed_documents(batch)
                    with self.lock:
                        self.require_present(document_id)
                        collection.upsert(ids=ids[offset:offset + 64], documents=batch,
                                          metadatas=metadata[offset:offset + 64], embeddings=vectors)
                stage = "verifying"
                current = self.file(document_id)
                if any(current[k] != file[k] for k in ("size_bytes", "modified_ns")):
                    raise DocumentError("The PDF changed during indexing. Retry the current version.")
                record.update(revision=revision, page_count=page_count, chunk_count=len(ids), chunk_ids=ids,
                              warning=warning, embedding_model=MODEL)
                if not self.indexed(record):
                    raise DocumentError("The written index could not be verified. Retry processing.")
                record.update(status="ready", stage="ready")
                self.write(record)
                log.info("PDF ready document=%s pages=%s chunks=%s", document_id, page_count, len(ids))
        except ProcessingCancelled:
            log.info("Discarded processing results for deleted document=%s", document_id)
        except Exception as error:
            log.exception("PDF processing failed document=%s stage=%s", document_id, stage)
            if record is not None:
                messages = {
                    "embedding": "Embeddings failed. Check sentence-transformers installation and model download/cache access, then retry.",
                    "chunking": "Text splitting failed. Check langchain-text-splitters installation and retry.",
                    "verifying": "The search index could not be verified. Check index storage and retry.",
                }
                record.update(status="failed", stage=stage, error=str(error) if isinstance(error, DocumentError)
                              else messages.get(stage, "PDF processing failed. Check the server log and retry."))
                try:
                    self.write(record)
                except ProcessingCancelled:
                    log.info("Did not restore metadata for deleted document=%s", document_id)
                except Exception:
                    log.exception("Could not persist PDF failure document=%s", document_id)
        finally:
            with self.lock:
                self.pending.discard(document_id)

    def active_documents(self, document_ids):
        if not document_ids:
            raise DocumentError("Select at least one ready PDF in Sources before chatting.", 400, "no_active_sources")
        available = {item["id"]: item for item in self.list_documents()}
        selected = []
        for document_id in dict.fromkeys(document_ids):
            item = available.get(document_id)
            if item is None:
                raise DocumentError("A selected PDF is missing. Refresh Sources and select an available PDF.", 404)
            if item["status"] != "ready":
                detail = item.get("error") or ("Wait for processing to finish." if item["status"] == "processing"
                                               else "Use Process/Retry in Sources.")
                raise DocumentError(f"{item['filename']} is {item['status']} (stage: {item.get('stage') or item['status']}). {detail}")
            selected.append(item)
        return selected

    def retrieve(self, question, document_ids):
        active = self.active_documents(document_ids)
        conditions = [{"$and": [{"document_id": item["id"]}, {"revision": item["revision"]}]} for item in active]
        where = conditions[0] if len(conditions) == 1 else {"$or": conditions}
        try:
            log.info("PDF retrieval started documents=%s", [item["id"] for item in active])
            result = self.collection().query(query_embeddings=[embeddings().embed_query(question)], where=where,
                n_results=min(6, sum(item["chunk_count"] for item in active)), include=["documents", "metadatas"])
            allowed, sources = {item["id"]: item for item in active}, []
            for text, meta in zip(result["documents"][0], result["metadatas"][0]):
                item = allowed.get(meta.get("document_id")) if meta else None
                if not item or meta.get("revision") != item["revision"] or meta.get("filename") != item["filename"]:
                    raise ValueError("Unexpected retrieval metadata")
                if not isinstance(meta.get("page"), int) or not 1 <= meta["page"] <= item["page_count"] or not isinstance(text, str) or not text.strip():
                    raise ValueError("Unusable retrieval content")
                sources.append(dict(label=f"S{len(sources) + 1}", document_id=item["id"], filename=item["filename"], page=meta["page"], text=text))
            if not sources:
                raise DocumentError("No text could be retrieved from the selected PDFs. Retry processing those sources.", 503, "retrieval_failed")
            log.info("PDF retrieval completed documents=%s passages=%s", list(allowed), len(sources))
            return active, sources
        except DocumentError:
            raise
        except Exception as error:
            log.exception("PDF retrieval failed for documents=%s: %s", [item["id"] for item in active], error)
            names = ", ".join(item["filename"] for item in active)
            raise DocumentError(f"Retrieval failed for {names}. Check the embedding model/index and retry these sources.", 503, "retrieval_failed") from error


@lru_cache(maxsize=8)
def _cached_library(data_dir, index_dir):
    return DocumentLibrary(data_dir, index_dir)


_library_lock = threading.Lock()


def get_library(data_dir=DATA_DIR, index_dir=DB_DIR):
    # Cache lookup and creation share one lock: concurrent first requests must
    # not construct separate pending-job registries for the same library.
    with _library_lock:
        return _cached_library(str(Path(data_dir).absolute()), str(Path(index_dir).absolute()))
