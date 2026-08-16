from .states import chatState
from langchain_groq import ChatGroq
from dotenv import load_dotenv

load_dotenv()
llm = ChatGroq(
    model="openai/gpt-oss-120b"
)
def chat_node(state : chatState):
    message = state['messages']

    response = llm.invoke(message)

    return {"messages": [response]}
