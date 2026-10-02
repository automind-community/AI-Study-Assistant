from .states import chatState
from langchain_groq import ChatGroq
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from .rag import get_retriever

load_dotenv()
llm = ChatGroq(
    model="llama-3.1-8b-instant"
)

def chat_node(state : chatState):
    messages = state['messages']
    
    # Get the latest human message
    last_message = messages[-1].content
    
    retriever = get_retriever()
    
    if retriever:
        docs = retriever.invoke(last_message)
        context = "\n\n".join(doc.page_content for doc in docs)
        
        system_prompt = f"You are a helpful AI assistant. Answer the user's questions based on the following context:\n\n{context}"
        
        # Prepend the system prompt with the context
        new_messages = [SystemMessage(content=system_prompt)] + messages
        response = llm.invoke(new_messages)
    else:
        response = llm.invoke(messages)

    return {"messages": [response]}
