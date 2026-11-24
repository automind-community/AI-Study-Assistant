## AI STUDY ASSISTANT MAIN FILE

## THANKS FOR CONTRIBUTING
from langchain_core.messages import HumanMessage
from chatbot.workflow import chat_workflow
from chatbot.documents import DocumentError, get_library




thread_id = 1

config = {"configurable" : {"thread_id": thread_id}}
ready_sources = [item for item in get_library().list_documents() if item["status"] == "ready"]
if not ready_sources:
    raise SystemExit("No indexed PDFs. Open the web workspace and process a PDF first.")
print("Using indexed sources:", ", ".join(item["filename"] for item in ready_sources))
while True:
    prompt = input(str("USER : "))
    if prompt == "exit":
        break
    else:
        try:
            response = chat_workflow.invoke({"messages": [HumanMessage(prompt)],
                "document_ids": [item["id"] for item in ready_sources]}, config=config)
            print(response['messages'][-1].content)
        except DocumentError as error:
            print(str(error))

        
print(chat_workflow.get_state(config=config))
