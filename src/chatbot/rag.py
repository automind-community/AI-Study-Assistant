import os

DATA_DIR = "data"
DB_DIR = "chroma_db"

def get_vectorstore():
    """Load existing vector store if it exists."""
    if not (os.path.exists(DB_DIR) and os.listdir(DB_DIR)):
        return None
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
        from langchain_community.vectorstores import Chroma
        embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        return Chroma(persist_directory=DB_DIR, embedding_function=embeddings)
    except Exception as e:
        print(f"Warning: Could not load vectorstore: {e}")
        return None

def process_and_embed_pdfs():
    """Process all PDFs in the data directory and create/update ChromaDB."""
    os.makedirs(DATA_DIR, exist_ok=True)
    pdf_files = [os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR) if f.endswith('.pdf')]
    
    if not pdf_files:
        return None
    
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_huggingface import HuggingFaceEmbeddings
    from langchain_community.vectorstores import Chroma

    documents = []
    for pdf_file in pdf_files:
        loader = PyPDFLoader(pdf_file)
        documents.extend(loader.load())
        
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    splits = text_splitter.split_documents(documents)
    
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    
    # Store in ChromaDB
    vectorstore = Chroma.from_documents(
        documents=splits, 
        embedding=embeddings, 
        persist_directory=DB_DIR
    )
    return vectorstore

def get_retriever():
    vectorstore = get_vectorstore()
    if vectorstore:
        return vectorstore.as_retriever(search_kwargs={"k": 3})
    return None

