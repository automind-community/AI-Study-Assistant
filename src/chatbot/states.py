from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


class chatState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    document_ids: list[str]
    data_dir: str
    index_dir: str
    active_documents: list[dict]
    citations: list[dict]
    answer_mode: str
    context_document_ids: list[str]
