import os
import shutil
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

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
