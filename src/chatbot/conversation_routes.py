"""Browser-session conversation API; PDF management remains in app/documents."""
import hashlib
import re
import secrets
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from .conversations import present_conversation, source_snapshot
from .documents import DocumentError

COOKIE_NAME = "study_session"


def attach_session_cookie(request, response):
    token = getattr(request.state, "new_session_token", None)
    if token:
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 365, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


def session_owner(request: Request, response: Response):
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD") and origin and origin != str(request.base_url).rstrip("/"):
        raise DocumentError("Use this workspace's own page for this action.", 403, "origin_mismatch")
    token = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        token = secrets.token_hex(32)
        request.state.new_session_token = token
    attach_session_cookie(request, response)
    return hashlib.sha256(token.encode()).hexdigest()


class CreateConversation(BaseModel):
    document_ids: list[str] = Field(default_factory=list, max_length=20)


class UpdateConversation(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    document_ids: list[str] | None = Field(default=None, max_length=20)


class LegacyMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant|error)$")
    text: str = Field(max_length=100000)
    time: int = Field(default=0, ge=0)
    citations: list[dict] = Field(default_factory=list, max_length=100)
    sources: list[str] = Field(default_factory=list, max_length=20)


class ImportConversation(BaseModel):
    legacy_id: str = Field(min_length=1, max_length=100)
    messages: list[LegacyMessage] = Field(max_length=1000)
    document_ids: list[str] = Field(default_factory=list, max_length=20)


def conversation_router(store_provider, library_provider):
    router = APIRouter(prefix="/conversations")

    @router.get("")
    def list_conversations(include_empty: bool = False, owner: str = Depends(session_owner)):
        return {"conversations": store_provider().list(owner, include_empty=include_empty)}

    @router.post("", status_code=201)
    def create_conversation(body: CreateConversation, owner: str = Depends(session_owner)):
        if body.document_ids:
            library_provider().active_documents(body.document_ids)
        return store_provider().create(owner, body.document_ids)

    @router.post("/import")
    def import_conversation(body: ImportConversation, owner: str = Depends(session_owner)):
        messages = []
        for message in body.messages:
            citations = [citation for citation in message.citations if
                isinstance(citation.get("document_id"), str) and isinstance(citation.get("filename"), str)
                and isinstance(citation.get("page"), int) and citation["page"] > 0
                and isinstance(citation.get("excerpt"), str) and re.fullmatch(r"S\d+", str(citation.get("label", "")))]
            ids = list(dict.fromkeys(citation["document_id"] for citation in citations))
            messages.append({"id": str(uuid.uuid4()), "role": message.role, "text": message.text,
                "time": message.time, "citations": citations, "source_ids": ids, "sources": message.sources,
                "answer_mode": "legacy" if ids or message.sources or re.search(r"\[S\d+\]", message.text, re.I) else "general", "imported": True})
        return store_provider().import_legacy(owner, body.legacy_id, messages, body.document_ids)

    @router.get("/{conversation_id}")
    def get_conversation(conversation_id: str, owner: str = Depends(session_owner)):
        item = store_provider().get(owner, conversation_id)
        return present_conversation(item, source_snapshot(library_provider()))

    @router.patch("/{conversation_id}")
    def update_conversation(conversation_id: str, body: UpdateConversation, owner: str = Depends(session_owner)):
        # Check ownership before examining source readiness.
        current = store_provider().get(owner, conversation_id)
        if body.document_ids:
            added = [key for key in body.document_ids if key not in current["document_ids"]]
            if added:
                library_provider().active_documents(added)
        return store_provider().update(owner, conversation_id, body.title, body.document_ids)

    @router.delete("/{conversation_id}")
    def delete_conversation(conversation_id: str, owner: str = Depends(session_owner)):
        return store_provider().delete(owner, conversation_id)

    return router
