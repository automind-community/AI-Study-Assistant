# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2026-09-28

### Added
- **FastAPI Web Backend**: Added a FastAPI application (`src/app.py`) to serve as the backend web server, replacing the need to rely solely on the CLI.
- **Hermes-Inspired Web Frontend**: Created a minimal and elegant UI (`src/templates/index.html`) using HTML/CSS/JS, with custom styling inspired by the Hermes brand (signature orange `#F37021`, Playfair Display serif fonts, and Inter sans-serif fonts).
- **PDF Upload Feature**: Users can now upload PDF files directly through the web interface.
- **Data Directory Structure**: The backend automatically creates a `data/` directory upon startup and saves all successfully uploaded PDFs into it.
- **Dependencies**: Added `fastapi`, `uvicorn`, `python-multipart`, and `jinja2` to `requirements.txt`.

### Fixed
- Fixed an `Internal Server Error` in FastAPI caused by a strict `TemplateResponse` signature requirement in modern versions of Starlette/FastAPI, ensuring the `request` argument is explicitly passed as a keyword argument.
