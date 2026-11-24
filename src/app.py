"""AI-assisted local study workspace; contribution policy is unchanged."""
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading

from fastapi import BackgroundTasks, Depends, FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

CURRENT_DIR = str(Path(__file__).resolve().parent)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from langchain_core.messages import HumanMessage
from chatbot.documents import DATA_DIR as DEFAULT_DATA_DIR, DB_DIR, DocumentError, get_library
from chatbot.conversations import context_messages, get_conversation_store, present_conversation, source_snapshot
from chatbot.conversation_routes import attach_session_cookie, conversation_router, session_owner
from chatbot.workflow import saved_chat_workflow as chat_workflow

app = FastAPI(title="AI Study Assistant")
app.mount("/static", StaticFiles(directory=str(Path(CURRENT_DIR) / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(CURRENT_DIR) / "templates"))


def static_version(path: str = "") -> str:
    """Return mtime integer for cache-busting static assets."""
    try:
        static_dir = Path(CURRENT_DIR) / "static"
        if path:
            file_path = static_dir / path.lstrip("/").split("?")[0]
            if file_path.is_file():
                return str(int(file_path.stat().st_mtime))
        mtimes = [f.stat().st_mtime for f in static_dir.glob("*") if f.is_file()]
        return str(int(max(mtimes))) if mtimes else "1"
    except Exception:
        return "1"


templates.env.globals["static_version"] = static_version
DATA_DIR = DEFAULT_DATA_DIR
INDEX_DIR = DB_DIR
log = logging.getLogger("uvicorn.error.study")


def library():
    return get_library(DATA_DIR, INDEX_DIR)


def conversations():
    return get_conversation_store(INDEX_DIR)


app.include_router(conversation_router(conversations, library))


def _warmup_model():
    """Warm the embedding model in the background so uploads index quickly without cold-start latency."""
    if any(mod.startswith("unittest") for mod in sys.modules):
        return
    try:
        from chatbot.documents import embeddings
        embeddings()
    except Exception:
        log.exception("Background embedding model warmup failed")


threading.Thread(target=_warmup_model, daemon=True, name="model-warmup").start()


@app.exception_handler(DocumentError)
async def document_error_handler(request: Request, error: DocumentError):
    return attach_session_cookie(request, JSONResponse(status_code=error.status_code,
        content={"error": str(error), "code": error.code}))


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/documents")
def list_documents():
    """Read file/index metadata; never extract or embed from a GET request."""
    try:
        return {"documents": library().list_documents()}
    except Exception:
        log.exception("Could not list document metadata")
        return JSONResponse(status_code=500, content={"error": "Unable to list documents."})


@app.post("/upload")
def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    service = library()
    try:
        document = service.save_upload(file.filename, file.file)
    except DocumentError:
        raise
    except Exception:
        log.exception("PDF save failed")
        return JSONResponse(status_code=500, content={"error": "Unable to save this PDF. Check storage and retry."})
    if document["status"] != "ready":
        try:
            if service.queue(document["id"]):
                background_tasks.add_task(service.process_document, document["id"])
        except Exception:
            log.exception("Could not queue saved PDF document=%s", document["id"])
            return JSONResponse(status_code=503, content={"saved": True, "filename": document["filename"],
                "error": "PDF saved, but processing could not start. Refresh Sources and use Process/Retry."})
        document = {**document, "status": "processing", "stage": "queued", "error": None}
    return JSONResponse(status_code=200 if document["status"] == "ready" else 202,
        content={"saved": True, "filename": document["filename"], "document": document,
                 "message": "PDF already indexed." if document["status"] == "ready" else "PDF saved. Processing has been queued."},
        background=background_tasks)


@app.post("/documents/{document_id}/process", status_code=202)
def process_document(document_id: str, background_tasks: BackgroundTasks):
    service = library()
    try:
        if service.queue(document_id):
            background_tasks.add_task(service.process_document, document_id)
        return {"document_id": document_id, "status": "processing", "message": "Processing queued."}
    except DocumentError:
        raise
    except Exception:
        log.exception("Could not queue PDF document=%s", document_id)
        return JSONResponse(status_code=503, content={"error": "Unable to start processing. Check storage and retry."})


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    thread_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    conversation_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    document_ids: list[str] | None = Field(default=None, max_length=20)


@app.post("/chat")
def chat_endpoint(body: ChatBody, request: Request, owner: str = Depends(session_owner)):
    if not body.message.strip():
        raise DocumentError("Enter a message.", 400, "empty_message")
    try:
        store = conversations()
        if body.conversation_id:
            conversation = store.get(owner, body.conversation_id)
        elif body.thread_id:
            # A legacy browser ID is only a lookup within this owner's history.
            # Missing legacy IDs start a new server-generated conversation.
            try:
                conversation = store.get(owner, body.thread_id)
            except DocumentError:
                conversation = store.create(owner, body.document_ids)
        else:
            conversation = store.create(owner, body.document_ids)
        with store.turn(owner, conversation["id"]) as conversation:
            document_ids = body.document_ids if body.document_ids is not None else conversation["document_ids"]
            available = source_snapshot(library())
            history, context_ids = context_messages(conversation, document_ids, available)
            conversation["document_ids"] = list(dict.fromkeys(document_ids))
            store.append(owner, conversation, "user", body.message.strip(), source_ids=document_ids)
            scope = json.dumps(sorted((key, (available or {}).get(key, {}).get("revision"),
                                       (available or {}).get(key, {}).get("status", "unverified")) for key in set(document_ids)))
            scoped_thread = owner[:16] + ":" + conversation["id"] + ":" + hashlib.sha256(scope.encode()).hexdigest()[:24]
            try:
                result = chat_workflow.invoke({
                    "messages": history + [HumanMessage(content=body.message.strip())],
                    "document_ids": document_ids, "data_dir": DATA_DIR, "index_dir": INDEX_DIR,
                    "active_documents": [], "citations": [], "answer_mode": "", "context_document_ids": context_ids,
                }, config={"configurable": {"thread_id": scoped_thread}})
                # Serialize publication with document deletion. A source removed
                # during the model request cannot produce a newly saved answer.
                with library().lock:
                    active = result["active_documents"]
                    if active:
                        current = library().active_documents([item["id"] for item in active])
                        if {(item["id"], item["revision"]) for item in current} != {(item["id"], item["revision"]) for item in active}:
                            raise DocumentError("A source changed while answering. Verify Sources and ask again.", 409)
                    store.append(owner, conversation, "assistant", result["messages"][-1].content,
                        answer_mode=result.get("answer_mode") or ("document" if result["citations"] else "general"),
                        citations=result["citations"], source_ids=[item["id"] for item in active] or result.get("context_document_ids", []),
                        source_revisions={item["id"]: item["revision"] for item in active},
                        sources=[item["filename"] for item in active] if result["citations"] else [])
            except Exception as error:
                text = str(error) if isinstance(error, DocumentError) else "Chat is temporarily unavailable. Check the server log and Groq configuration, then retry."
                store.append(owner, conversation, "error", text, source_ids=document_ids)
                raise
            return {"response": result["messages"][-1].content, "thread_id": conversation["id"],
                    "conversation_id": conversation["id"], "active_documents": result["active_documents"],
                    "citations": result["citations"], "conversation": present_conversation(conversation, source_snapshot(library()))}
    except DocumentError:
        raise
    except Exception:
        log.exception("Chat failed for selected documents=%s", body.document_ids)
        return attach_session_cookie(request, JSONResponse(status_code=503,
            content={"error": "Chat is temporarily unavailable. Check the server log and Groq configuration, then retry."}))


@app.delete("/documents/{document_id}")
def delete_document(document_id: str, owner: str = Depends(session_owner)):
    # No client filename/path is accepted. Conversations are archived separately.
    return library().delete_document(document_id)


@app.get("/health")
def health_endpoint():
    """Server reachability is independent of PDF storage and index readiness."""
    return {"status": "online"}


@app.get("/status")
def status_endpoint():
    try:
        documents = library().list_documents()
        counts = {state: sum(item["status"] == state for item in documents)
                  for state in ("saved", "processing", "ready", "failed")}
        return {"status": "online", "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
                "has_documents": counts["ready"] > 0, "documents": counts}
    except Exception:
        log.exception("Could not verify document readiness")
        return JSONResponse(status_code=503, content={"status": "degraded", "has_documents": False,
                                                       "error": "Document readiness could not be verified."})
