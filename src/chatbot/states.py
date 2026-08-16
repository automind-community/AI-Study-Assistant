from typing import TypedDict, Literal, Annotated
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

class chatState(TypedDict):
    messages : Annotated[list[BaseMessage], add_messages]


