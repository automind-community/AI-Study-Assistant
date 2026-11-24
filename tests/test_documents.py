"""Focused offline regression suite. No provider requests, downloads or real data."""
from contextlib import contextmanager, ExitStack
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
import stat
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def pdf_bytes(text="A reliable study fact: the project code is ORBIT-314."):
    """Small actual one-page PDF with a text layer; no PDF-generation dependency."""
    text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    result, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


class MemoryCollection:
    """Test-only explicit-vector collection, including real metadata filters."""
    def __init__(self):
        self.items = {}
        self.upsert_calls = 0
        self.last_where = None

    def upsert(self, ids, documents, metadatas, embeddings):
        self.upsert_calls += 1
        for key, text, metadata in zip(ids, documents, metadatas):
            self.items[key] = (text, metadata)

    def get(self, ids, include):
        keys = [key for key in ids if key in self.items]
        return {"ids": keys, "metadatas": [self.items[key][1] for key in keys]}

    @staticmethod
    def matches(metadata, condition):
        if "$and" in condition:
            return all(MemoryCollection.matches(metadata, item) for item in condition["$and"])
        if "$or" in condition:
            return any(MemoryCollection.matches(metadata, item) for item in condition["$or"])
        return all(metadata.get(key) == value for key, value in condition.items())

    def query(self, query_embeddings, where, n_results, include):
        self.last_where = where
        hits = [item for item in self.items.values() if self.matches(item[1], where)][:n_results]
        return {"documents": [[item[0] for item in hits]], "metadatas": [[item[1] for item in hits]]}

    def delete(self, where):
        for key in list(self.items):
            if self.matches(self.items[key][1], where):
                del self.items[key]


@contextmanager
def isolated_app():
    with TemporaryDirectory(prefix="study-regression-") as directory, ExitStack() as stack:
        stack.enter_context(patch.object(sys, "path", sys.path.copy()))
        # Only isolate the test-specific modules we override. Using patch.dict(sys.modules)
        # would remove *all* newly imported modules on teardown, including numpy's C extension
        # which cannot be re-loaded in the same process (ImportError: cannot load module more
        # than once per process). Instead, save and restore only our synthetic module names.
        _isolated = ('chatbot', 'chatbot.documents', 'chatbot.workflow',
                     'chatbot.states', 'chatbot.nodes', 'study_test_app')
        _saved = {k: sys.modules[k] for k in _isolated if k in sys.modules}
        def _restore_modules():
            for k in list(sys.modules):
                if k in _isolated or (k.startswith('chatbot.') and k not in _saved):
                    sys.modules.pop(k, None)
            sys.modules.update(_saved)
        stack.callback(_restore_modules)
        documents = load_module("chatbot.documents", ROOT / "src/chatbot/documents.py")
        chatbot = ModuleType("chatbot")
        chatbot.__path__ = [str(ROOT / "src/chatbot")]
        workflow = ModuleType("chatbot.workflow")
        workflow.chat_workflow = Mock(name="isolated_workflow")
        workflow.saved_chat_workflow = workflow.chat_workflow
        chatbot.documents, chatbot.workflow = documents, workflow
        sys.modules.update({"chatbot": chatbot, "chatbot.workflow": workflow})
        data_dir, index_dir = Path(directory) / "data", Path(directory) / "index"
        data_dir.mkdir()
        service = documents.DocumentLibrary(data_dir, index_dir)
        collection = MemoryCollection()
        service.collection = Mock(return_value=collection)
        model = Mock()
        model.embed_documents.side_effect = lambda texts: [[float(len(text)), 1.0] for text in texts]
        model.embed_query.return_value = [1.0, 1.0]
        stack.enter_context(patch.object(documents, "embeddings", return_value=model))
        module = load_module("study_test_app", ROOT / "src/app.py")
        module.DATA_DIR, module.INDEX_DIR = str(data_dir), str(index_dir)
        stack.enter_context(patch.object(module, "library", return_value=service))
        # Router closures keep their original provider function. Its underlying
        # factory must resolve to this same temporary, explicit-vector library.
        stack.enter_context(patch.object(module, "get_library", return_value=service))
        yield SimpleNamespace(module=module, documents=documents, service=service, collection=collection,
                              model=model, data_dir=data_dir, index_dir=index_dir, workflow=workflow.chat_workflow)


class DocumentsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.enterContext(isolated_app())
        self.client = self.enterContext(TestClient(self.fixture.module.app))
        self.service = self.fixture.service

    def upload(self, name="notes.pdf", content=None):
        return self.client.post("/upload", files={"file": (name, pdf_bytes() if content is None else content, "application/pdf")})

    def documents(self):
        response = self.client.get("/documents")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(self.fixture.data_dir), response.text)
        return response.json()["documents"]

    def ready(self, name="notes.pdf", text="The project code is ORBIT-314."):
        response = self.upload(name, pdf_bytes(text))
        self.assertEqual(response.status_code, 202)
        document = next(item for item in self.documents() if item["filename"] == name)
        self.assertEqual(document["status"], "ready")
        return document

    def test_empty_and_missing_directory(self):
        self.assertEqual(self.documents(), [])
        self.fixture.data_dir.rmdir()
        self.assertEqual(self.documents(), [])
        self.assertFalse(self.fixture.data_dir.exists())

    def test_saved_is_not_ready_and_mixed_case_order_and_sizes(self):
        files = {"zebra.PDF": b"abc", "Alpha.pdf": b"", "beta.PdF": b"12345"}
        for name, content in files.items():
            (self.fixture.data_dir / name).write_bytes(content)
        (self.fixture.data_dir / "ignore.txt").write_text("ignore")
        folder = self.fixture.data_dir / "folder.pdf"
        folder.mkdir()
        (folder / "nested.pdf").write_bytes(pdf_bytes())
        result = self.documents()
        self.assertEqual([item["filename"] for item in result], ["Alpha.pdf", "beta.PdF", "zebra.PDF"])
        self.assertEqual([item["size_bytes"] for item in result], [0, 5, 3])
        self.assertTrue(all(item["status"] == "saved" for item in result))
        self.fixture.model.embed_documents.assert_not_called()
        self.assertFalse(self.client.get("/status").json()["has_documents"])

    def test_symbolic_links_excluded(self):
        import os
        target = self.fixture.data_dir.parent / "external.pdf"
        target.write_bytes(pdf_bytes())
        try:
            os.symlink(target, self.fixture.data_dir / "link.pdf")
            os.symlink(target.parent / "missing.pdf", self.fixture.data_dir / "broken.PDF")
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable; enable Developer Mode on Windows")
        self.assertEqual(self.documents(), [])

    def test_safe_filesystem_error(self):
        with patch.object(self.service, "files", side_effect=OSError("private/internal/path")):
            response = self.client.get("/documents")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "Unable to list documents."})

    def test_real_text_extraction_and_page_metadata(self):
        document = self.ready()
        self.assertEqual(document["page_count"], 1)
        self.assertGreater(document["chunk_count"], 0)
        active, sources = self.service.retrieve("What is the project code?", [document["id"]])
        self.assertIn("ORBIT-314", sources[0]["text"])
        self.assertEqual((sources[0]["filename"], sources[0]["page"]), ("notes.pdf", 1))
        self.assertTrue(self.client.get("/status").json()["has_documents"])

    def test_empty_corrupt_and_image_only_pdfs_fail_honestly(self):
        for name, content in (("empty.pdf", b""), ("corrupt.pdf", b"not PDF"), ("scanned.pdf", pdf_bytes(""))):
            with self.subTest(name=name):
                self.upload(name, content)
                item = next(item for item in self.documents() if item["filename"] == name)
                self.assertEqual(item["status"], "failed")
                self.assertTrue(item["error"])
                self.assertTrue((self.fixture.data_dir / name).exists())
                if name == "scanned.pdf":
                    self.assertIn("OCR", item["error"])

    def test_encrypted_pdf_reports_unlock_action(self):
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.append(PdfReader(BytesIO(pdf_bytes())))
        writer.encrypt("offline-test-password")
        output = BytesIO()
        writer.write(output)
        self.upload("encrypted.pdf", output.getvalue())
        item = self.documents()[0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("encrypted", item["error"])

    def test_failed_pdf_does_not_prevent_valid_pdf(self):
        self.upload("bad.pdf", b"invalid")
        good = self.ready("good.PDF")
        self.assertEqual(good["status"], "ready")
        self.assertEqual([item["status"] for item in self.documents()], ["failed", "ready"])

    def test_embedding_failure_and_retry(self):
        self.fixture.model.embed_documents.side_effect = RuntimeError("private model path")
        self.upload()
        failed = self.documents()[0]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Embeddings failed", failed["error"])
        self.assertNotIn("private model path", failed["error"])
        self.fixture.model.embed_documents.side_effect = lambda texts: [[1.0, 1.0] for _ in texts]
        response = self.client.post(f"/documents/{failed['id']}/process")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.documents()[0]["status"], "ready")

    def test_repeat_upload_and_reprocess_are_idempotent(self):
        item = self.ready()
        count, calls = len(self.fixture.collection.items), self.fixture.model.embed_documents.call_count
        self.assertEqual(self.upload(content=pdf_bytes("The project code is ORBIT-314.")).status_code, 200)
        self.client.post(f"/documents/{item['id']}/process")
        self.assertEqual(len(self.fixture.collection.items), count)
        self.assertEqual(self.fixture.model.embed_documents.call_count, calls)

    def test_different_content_preserves_same_name_upload(self):
        original = pdf_bytes("First document")
        self.upload(content=original)
        self.upload(content=pdf_bytes("Second document"))
        self.assertEqual((self.fixture.data_dir / "notes.pdf").read_bytes(), original)
        self.assertEqual(len(self.documents()), 2)

    def test_path_upload_rejected(self):
        response = self.upload("../escape.pdf")
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.fixture.data_dir.parent / "escape.pdf").exists())

    def test_source_switching_filters_retrieval(self):
        first = self.ready("a.pdf", "Only A contains ORBIT-314.")
        second = self.ready("b.pdf", "Only B contains FOREST-829.")
        _, sources = self.service.retrieve("project code", [second["id"]])
        self.assertTrue(all(item["document_id"] == second["id"] for item in sources))
        self.assertNotIn("ORBIT-314", " ".join(item["text"] for item in sources))
        _, both = self.service.retrieve("project codes", [first["id"], second["id"]])
        self.assertEqual({item["document_id"] for item in both}, {first["id"], second["id"]})

    def test_query_failure_is_document_specific(self):
        item = self.ready()
        with patch.object(self.fixture.collection, "query", side_effect=RuntimeError("private details")):
            with self.assertRaises(self.fixture.documents.DocumentError) as caught:
                self.service.retrieve("question", [item["id"]])
        self.assertIn("notes.pdf", str(caught.exception))
        self.assertNotIn("private details", str(caught.exception))
        self.fixture.workflow.invoke.assert_not_called()

    def test_index_loss_never_reports_ready(self):
        self.ready()
        self.fixture.collection.items.clear()
        self.assertEqual(self.documents()[0]["status"], "failed")
        self.assertFalse(self.client.get("/status").json()["has_documents"])

    def test_restart_preserves_verified_state_and_detects_interruption(self):
        item = self.ready()
        restarted = self.fixture.documents.DocumentLibrary(self.fixture.data_dir, self.fixture.index_dir)
        restarted.collection = Mock(return_value=self.fixture.collection)
        self.assertEqual(restarted.list_documents()[0]["status"], "ready")
        self.service.queue(item["id"])
        self.assertEqual(self.service.list_documents()[0]["status"], "processing")
        self.assertEqual(restarted.list_documents()[0]["status"], "failed")
        self.assertIn("interrupted", restarted.list_documents()[0]["error"])

    def test_changed_file_requires_processing_again(self):
        self.ready()
        (self.fixture.data_dir / "notes.pdf").write_bytes(pdf_bytes("A changed PDF with different length"))
        self.assertEqual(self.documents()[0]["status"], "saved")

    def test_chat_accepts_general_messages_without_sources(self):
        self.fixture.workflow.invoke.return_value = {
            "messages": [AIMessage(content="Hello!")], "active_documents": [], "citations": []}
        response = self.client.post("/chat", json={"message": "Hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active_documents"], [])
        self.assertEqual(response.json()["citations"], [])
        self.assertEqual(self.fixture.workflow.invoke.call_args.args[0]["document_ids"], [])

    def test_chat_still_rejects_whitespace(self):
        response = self.client.post("/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 400)
        self.fixture.workflow.invoke.assert_not_called()

    def test_health_is_independent_of_document_storage(self):
        with patch.object(self.service, "list_documents", side_effect=OSError("storage unavailable")) as listing:
            response = self.client.get("/health")
        self.assertEqual(response.json(), {"status": "online"})
        listing.assert_not_called()

    def test_chat_thread_is_scoped_by_selected_revision(self):
        first, second = self.ready("a.pdf"), self.ready("b.pdf")
        self.fixture.workflow.invoke.return_value = {"messages": [AIMessage(content="Answer [S1]")], "active_documents": [first], "citations": []}
        body = {"message": "question", "thread_id": "same-thread", "document_ids": [first["id"]]}
        first_response = self.client.post("/chat", json=body)
        self.assertEqual(first_response.status_code, 200)
        body["conversation_id"] = first_response.json()["conversation_id"]
        first_thread = self.fixture.workflow.invoke.call_args.kwargs["config"]["configurable"]["thread_id"]
        body["document_ids"] = [second["id"]]
        self.client.post("/chat", json=body)
        second_thread = self.fixture.workflow.invoke.call_args.kwargs["config"]["configurable"]["thread_id"]
        self.assertNotEqual(first_thread, second_thread)
        self.assertEqual(self.fixture.workflow.invoke.call_args.args[0]["document_ids"], [second["id"]])

    def load_nodes(self, route="document"):
        states = ModuleType("chatbot.states")
        states.chatState = dict
        sys.modules["chatbot.states"] = states
        with patch("dotenv.load_dotenv", return_value=False):
            nodes = load_module("chatbot.nodes", ROOT / "src/chatbot/nodes.py")
        self.enterContext(patch.object(nodes, "get_library", return_value=self.service))
        if route is not None:
            self.enterContext(patch.object(nodes, "classify_question", return_value=nodes.ChatIntent(mode=route)))
        return nodes

    def test_greeting_with_no_sources_or_unavailable_metadata_needs_no_model(self):
        nodes = self.load_nodes()
        self.fixture.workflow.invoke.side_effect = lambda state, config: nodes.chat_node(state)
        with patch.object(self.service, "list_documents", side_effect=OSError("unavailable")):
            with patch.object(nodes, "get_llm") as model:
                response = self.client.post("/chat", json={"message": "Hello"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Hi!", response.json()["response"])
        self.assertEqual(response.json()["citations"], [])
        model.assert_not_called()

    def test_general_answer_never_retrieves_or_reuses_document_history(self):
        item = self.ready()
        nodes = self.load_nodes(route="general")
        model = Mock()
        model.invoke.return_value = AIMessage(content="Photosynthesis uses light to make sugars.")
        messages = [HumanMessage(content="What is in notes.pdf?"),
                    AIMessage(content="PRIVATE_PDF_FACT [S1]", additional_kwargs={"answer_mode": "document"}),
                    HumanMessage(content="Explain photosynthesis in general.")]
        with patch.object(self.service, "retrieve") as retrieve, patch.object(nodes, "get_llm", return_value=model):
            result = nodes.chat_node({"messages": messages, "document_ids": [item["id"]]})
        retrieve.assert_not_called()
        self.assertNotIn("PRIVATE_PDF_FACT", " ".join(message.content for message in model.invoke.call_args.args[0]))
        self.assertEqual(result["active_documents"], [])
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["answer_mode"], "general")

    def test_general_study_answer_without_pdfs_or_verified_library(self):
        nodes = self.load_nodes(route="general")
        model = Mock()
        model.invoke.return_value = AIMessage(content="Try spaced practice and short review sessions.")
        self.fixture.workflow.invoke.side_effect = lambda state, config: nodes.chat_node(state)
        with patch.object(self.service, "list_documents", side_effect=OSError("unavailable")):
            with patch.object(nodes, "get_llm", return_value=model):
                response = self.client.post("/chat", json={"message": "How can I revise effectively?"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["citations"], [])
        self.assertEqual(response.json()["active_documents"], [])

    def test_general_answer_preserves_general_history(self):
        nodes = self.load_nodes(route="general")
        model = Mock()
        model.invoke.return_value = AIMessage(content="Use flashcards for active recall.")
        messages = [HumanMessage(content="Help me study biology."),
                    AIMessage(content="Try active recall.", additional_kwargs={"answer_mode": "general"}),
                    HumanMessage(content="How do I do that?")]
        with patch.object(nodes, "get_llm", return_value=model):
            nodes.chat_node({"messages": messages, "document_ids": []})
        self.assertIn("Help me study biology.", [message.content for message in model.invoke.call_args.args[0]])

    def test_general_model_cannot_fabricate_pdf_citation(self):
        nodes = self.load_nodes(route="general")
        model = Mock()
        model.invoke.return_value = AIMessage(content="An invented PDF answer [S1].")
        with patch.object(nodes, "get_llm", return_value=model):
            with self.assertRaises(self.fixture.documents.DocumentError) as caught:
                nodes.chat_node({"messages": [HumanMessage(content="Explain photosynthesis.")], "document_ids": []})
        self.assertEqual(caught.exception.code, "invalid_citation")

    def test_named_unavailable_pdf_reports_real_status_even_with_other_ready_selection(self):
        ready = self.ready("ready.pdf")
        self.upload("failed.pdf", b"not a PDF")
        processing = self.ready("processing.pdf")
        self.service.queue(processing["id"])
        nodes = self.load_nodes()
        with patch.object(nodes, "get_llm") as model, patch.object(self.service, "retrieve") as retrieve:
            for name, status, stage in (("failed.pdf", "failed", "extracting"),
                                        ("processing.pdf", "processing", "queued")):
                with self.subTest(filename=name):
                    result = nodes.chat_node({"messages": [HumanMessage(content=f"Summarize {name}.")],
                                              "document_ids": [ready["id"]]})
                    self.assertIn(name, result["messages"][0].content)
                    self.assertIn(status, result["messages"][0].content)
                    self.assertIn(stage, result["messages"][0].content)
                    self.assertEqual(result["citations"], [])
                    self.assertEqual(result["active_documents"], [])
        model.assert_not_called()
        retrieve.assert_not_called()

    def test_missing_or_unselected_named_pdf_cannot_use_another_selected_source(self):
        ready, other = self.ready("ready.pdf"), self.ready("other.pdf")
        nodes = self.load_nodes()
        with patch.object(nodes, "get_llm") as model, patch.object(self.service, "retrieve") as retrieve:
            for name in ("missing.pdf", "not-ready.pdf", other["filename"]):
                result = nodes.chat_node({"messages": [HumanMessage(content=f"Summarize {name}.")],
                                          "document_ids": [ready["id"]]})
                self.assertEqual(result["citations"], [])
                self.assertEqual(result["active_documents"], [])
        model.assert_not_called()
        retrieve.assert_not_called()

    def test_selected_pdf_losing_readiness_blocks_document_followup(self):
        item = self.ready()
        self.service.queue(item["id"])
        nodes = self.load_nodes()
        with patch.object(nodes, "get_llm") as model:
            with self.assertRaises(self.fixture.documents.DocumentError) as caught:
                nodes.chat_node({"messages": [HumanMessage(content="Summarize it.")], "document_ids": [item["id"]]})
        self.assertIn("notes.pdf is processing", str(caught.exception))
        self.assertIn("queued", str(caught.exception))
        model.assert_not_called()

    def test_pdf_question_with_no_selection_reports_uploaded_statuses(self):
        self.upload("failed.pdf", b"not a PDF")
        nodes = self.load_nodes()
        with patch.object(nodes, "get_llm") as model:
            result = nodes.chat_node({"messages": [HumanMessage(content="Summarize my PDF.")], "document_ids": []})
        self.assertIn("failed.pdf: failed", result["messages"][0].content)
        self.assertIn("could not be read", result["messages"][0].content)
        model.assert_not_called()

    def test_document_question_requires_verified_metadata(self):
        nodes = self.load_nodes()
        with patch.object(self.service, "list_documents", side_effect=OSError("unavailable")):
            with patch.object(nodes, "get_llm") as model:
                with self.assertRaises(self.fixture.documents.DocumentError) as caught:
                    nodes.chat_node({"messages": [HumanMessage(content="Summarize my PDF.")], "document_ids": []})
        self.assertIn("status could not be verified", str(caught.exception))
        model.assert_not_called()

    def test_intent_router_rejects_unknown_document_ids(self):
        nodes = self.load_nodes(route=None)
        model = Mock()
        model.with_structured_output.return_value.invoke.return_value = nodes.ChatIntent(
            mode="document", referenced_document_ids=["unknown"])
        with patch.object(nodes, "get_llm", return_value=model):
            with self.assertRaises(self.fixture.documents.DocumentError) as caught:
                nodes.classify_question({"messages": [HumanMessage(content="Explain it.")], "document_ids": []}, [])
        self.assertEqual(caught.exception.code, "invalid_chat_intent")

    def test_intent_router_recognizes_pdf_title_without_extension(self):
        self.upload("biology textbook.pdf", b"not a PDF")
        item = self.documents()[0]
        nodes = self.load_nodes(route=None)
        model = Mock()
        model.with_structured_output.return_value.invoke.return_value = nodes.ChatIntent(
            mode="document", referenced_document_ids=[item["id"]])
        with patch.object(nodes, "get_llm", return_value=model):
            result = nodes.chat_node({"messages": [HumanMessage(content="Summarize biology textbook.")], "document_ids": []})
        model.invoke.assert_not_called()
        self.assertIn("biology textbook.pdf: failed", result["messages"][0].content)

    def test_availability_answer_uses_verified_metadata_without_model(self):
        item = self.ready()
        nodes = self.load_nodes()
        with patch.object(nodes, "get_llm") as model:
            for question in ("Which PDF is attached?", "Can you read my PDF?", "Do you have any documents?", "List my documents."):
                with self.subTest(question=question):
                    result = nodes.chat_node({"messages": [HumanMessage(content=question)], "document_ids": [item["id"]]})
                    self.assertIn("notes.pdf", result["messages"][0].content)
                    self.assertEqual(result["citations"], [])
        model.assert_not_called()

    def test_chat_node_passes_actual_context_and_returns_real_citation(self):
        item = self.ready()
        nodes = self.load_nodes()
        model = Mock()
        model.invoke.return_value = AIMessage(content="The project code is ORBIT-314 [S1].")
        with patch.object(nodes, "get_llm", return_value=model):
            result = nodes.chat_node({"messages": [HumanMessage(content="What is the project code?")], "document_ids": [item["id"]]})
        self.assertIn("ORBIT-314", model.invoke.call_args.args[0][0].content)
        self.assertEqual(result["citations"][0]["filename"], "notes.pdf")
        self.assertEqual(result["citations"][0]["page"], 1)
        self.assertIn("ORBIT-314", result["citations"][0]["excerpt"])

    def test_named_ready_pdf_uses_only_that_selected_source(self):
        first = self.ready("first.pdf", "Only the first source contains ORBIT-314.")
        second = self.ready("second.pdf", "Only the second source contains FOREST-829.")
        nodes = self.load_nodes()
        model = Mock()
        model.invoke.return_value = AIMessage(content="The code is FOREST-829 [S1].")
        with patch.object(nodes, "get_llm", return_value=model):
            result = nodes.chat_node({"messages": [HumanMessage(content="What is the code in second.pdf?")],
                                      "document_ids": [first["id"], second["id"]]})
        self.assertEqual([item["id"] for item in result["active_documents"]], [second["id"]])
        self.assertTrue(all(item["document_id"] == second["id"] for item in result["citations"]))
        self.assertNotIn("ORBIT-314", model.invoke.call_args.args[0][0].content)

    def test_fabricated_citation_rejected(self):
        item = self.ready()
        nodes = self.load_nodes()
        model = Mock()
        model.invoke.return_value = AIMessage(content="An answer [S999].")
        with patch.object(nodes, "get_llm", return_value=model):
            with self.assertRaises(self.fixture.documents.DocumentError):
                nodes.chat_node({"messages": [HumanMessage(content="A question")], "document_ids": [item["id"]]})

    def test_rendered_page_and_static_workspace_exist(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/workspace.js").status_code, 200)
        self.assertEqual(self.client.get("/static/workspace.css").status_code, 200)

    def test_portable_casefold_ties_and_nonregular_metadata(self):
        entries = []
        for name, mode in (("notes.pdf", stat.S_IFREG), ("Notes.PDF", stat.S_IFREG),
                           ("NOTES.pdf", stat.S_IFREG), ("link.pdf", stat.S_IFLNK),
                           ("folder.pdf", stat.S_IFDIR), ("pipe.pdf", stat.S_IFIFO)):
            entry = Mock()
            entry.name = name
            entry.stat.return_value = SimpleNamespace(st_mode=mode, st_size=42, st_mtime_ns=100)
            entries.append(entry)
        scanner = MagicMock()
        scanner.__iter__ = Mock(return_value=iter(entries))
        with patch.object(self.fixture.documents.os, "scandir", return_value=scanner):
            self.assertEqual([item["filename"] for item in self.documents()], ["NOTES.pdf", "Notes.PDF", "notes.pdf"])
        for entry in entries:
            entry.stat.assert_called_once_with(follow_symlinks=False)

    def test_blank_pages_preserve_physical_page_citations(self):
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=200)
        writer.append(PdfReader(BytesIO(pdf_bytes("Evidence on physical page two."))))
        output = BytesIO()
        writer.write(output)
        self.upload(content=output.getvalue())
        item = self.documents()[0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["page_count"], 2)
        self.assertIn("1 page", item["warning"])
        _, sources = self.service.retrieve("Where is the evidence?", [item["id"]])
        self.assertTrue(all(source["page"] == 2 for source in sources))

    def test_chat_node_does_not_fall_back_after_retrieval_error(self):
        item = self.ready()
        nodes = self.load_nodes()
        with patch.object(self.fixture.collection, "query", side_effect=OSError("private index error")):
            with patch.object(nodes, "get_llm") as model:
                with self.assertRaises(self.fixture.documents.DocumentError):
                    nodes.chat_node({"messages": [HumanMessage(content="A question")], "document_ids": [item["id"]]})
        model.assert_not_called()

    def test_uncited_model_answer_is_rejected(self):
        item = self.ready()
        nodes = self.load_nodes()
        model = Mock()
        model.invoke.return_value = AIMessage(content="An unsupported assertion.")
        with patch.object(nodes, "get_llm", return_value=model):
            with self.assertRaises(self.fixture.documents.DocumentError):
                nodes.chat_node({"messages": [HumanMessage(content="A question")], "document_ids": [item["id"]]})

    def test_legacy_index_files_are_preserved_and_do_not_mean_ready(self):
        self.fixture.index_dir.mkdir()
        legacy = self.fixture.index_dir / "chroma.sqlite3"
        legacy.write_bytes(b"legacy-index-sentinel")
        self.assertFalse(self.client.get("/status").json()["has_documents"])
        self.ready()
        self.assertEqual(legacy.read_bytes(), b"legacy-index-sentinel")


if __name__ == "__main__":
    unittest.main()
