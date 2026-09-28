# Project Architecture

This document describes how the AI Study Assistant is built and how its components interact.

## Current Stack (Phase 1)

### 1. Frontend (Web UI)
- **Tech**: Vanilla HTML, CSS, JavaScript + Jinja2 Templates.
- **Design**: Hermes-inspired elegant layout, utilizing a color palette of white, dark charcoal, and signature orange (`#F37021`). Uses Google Fonts (Playfair Display for headings, Inter for body text).
- **Functionality**: Provides a clean user interface restricted to `.pdf` files. Handles asynchronous uploads via JavaScript `fetch` to the backend and displays interactive loading and success states.

### 2. Backend Server
- **Tech**: FastAPI (Python), Uvicorn.
- **Routing**:
  - `GET /`: Serves the main `index.html` template.
  - `POST /upload`: An endpoint that receives `multipart/form-data`, validates that the file is a PDF, and saves the file stream to the local disk.
- **Storage**: Uploaded files are streamed and saved locally into the `data/` directory at the project root.

### 3. AI Chatbot Foundation (WIP)
- **Tech**: LangChain, LangGraph, ChatGroq.
- **Core Flow**:
  - `src/main.py` initiates a terminal-based CLI (to be integrated into the web UI in the future).
  - `src/chatbot/states.py` defines a `chatState` using LangGraph to maintain conversation history.
  - `src/chatbot/nodes.py` utilizes a `ChatGroq` language model to generate responses based on the current context.
  - `src/chatbot/workflow.py` ties the nodes into a `StateGraph` state machine with memory persistence.

## File Upload Workflow
```text
User (Browser) 
  -> Selects PDF 
  -> POST /upload (FormData) 
  -> FastAPI Endpoint 
  -> Validates extension 
  -> Writes stream to `data/filename.pdf` 
  -> Returns 200 JSON Success Response
```