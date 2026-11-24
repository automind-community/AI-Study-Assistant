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
# The web API supplies its owner-scoped SQLite history on every request. Keep
# process-local CLI memory separate so deleted chats leave no web checkpoints.
saved_chat_workflow = graph.compile()


