"""Compatibility entry points for the per-document RAG pipeline."""
from .documents import DATA_DIR, DB_DIR, get_library


def process_and_embed_pdfs():
    """Process independently so one failed PDF cannot abort the remaining files."""
    library = get_library(DATA_DIR, DB_DIR)
    for document in library.list_documents():
        if library.queue(document["id"]):
            library.process_document(document["id"])
    return library.list_documents()
