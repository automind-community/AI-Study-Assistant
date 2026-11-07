import os
import sys
import uuid
import shutil
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# Ensure src directory is available in sys.path for submodule imports
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from langchain_core.messages import HumanMessage
from chatbot.workflow import chat_workflow

app = FastAPI(title="AI Study Assistant")

# We will look for templates in the src/templates folder
templates = Jinja2Templates(directory="src/templates")

# Define where to save the files
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the main frontend UI."""
    return templates.TemplateResponse(request=request, name="index.html")

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Handle PDF uploads and save them to the data directory."""
    if not file.filename.lower().endswith('.pdf'):
        return {"error": "Only PDF files are supported"}
    
    # Save the file to the data directory
    file_location = os.path.join(DATA_DIR, file.filename)
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Trigger embedding processing
    try:
        from chatbot.rag import process_and_embed_pdfs
        process_and_embed_pdfs()
        return {"filename": file.filename, "message": "File uploaded and processed into vector database successfully"}
    except Exception as e:
        return {"filename": file.filename, "message": f"File uploaded but error during processing: {str(e)}"}


@app.post("/chat")
async def chat_endpoint(request: Request):
    """Process a chat message through the AI study assistant workflow."""
    thread_id = None
    try:
        body = await request.json()
        message = body.get("message")
        thread_id = body.get("thread_id") or str(uuid.uuid4())

        if not message:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing 'message' field in request body", "thread_id": thread_id},
            )

        config = {"configurable": {"thread_id": thread_id}}
        response = chat_workflow.invoke({"messages": [HumanMessage(content=message)]}, config=config)
        ai_reply = response["messages"][-1].content

        return {"response": ai_reply, "thread_id": thread_id}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "thread_id": thread_id},
        )


@app.get("/status")
async def status_endpoint():
    """Return the current status of the AI Study Assistant service."""
    chroma_dir = "chroma_db"
    chroma_has_docs = os.path.isdir(chroma_dir) and len(os.listdir(chroma_dir)) > 0

    data_has_pdfs = any(
        f.lower().endswith(".pdf") for f in os.listdir(DATA_DIR)
    ) if os.path.isdir(DATA_DIR) else False

    model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    return {
        "status": "online",
        "model": model_name,
        "has_documents": chroma_has_docs or data_has_pdfs,
    }
