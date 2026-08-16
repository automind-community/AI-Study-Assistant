from langgraph.graph import StateGraph, START, END
from .states import chatState
from langgraph.checkpoint.memory import MemorySaver
from .nodes import chat_node

checkpointer = MemorySaver()
graph = StateGraph(chatState)
# Nodes 
graph.add_node("chat_node", chat_node)



# Edges
graph.add_edge(START, "chat_node")
graph.add_edge("chat_node", END)


chat_workflow = graph.compile(checkpointer=checkpointer)


