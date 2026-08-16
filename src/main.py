## AI STUDY ASSISTANT MAIN FILE

## THANKS FOR CONTRIBUTING
from langchain_core.messages import HumanMessage
from chatbot.workflow import chat_workflow




thread_id = 1

config = {"configurable" : {"thread_id": thread_id}}
while True:
    prompt = input(str("USER : "))
    if prompt == "exit":
        break
    else:
        response = chat_workflow.invoke({"messages" :HumanMessage(prompt)}, config=config)
        print(response['messages'][-1].content)

        
print(chat_workflow.get_state(config=config))