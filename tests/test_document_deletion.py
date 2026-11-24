"""Deletion regressions use only isolated temporary PDFs/indexes. Not executed by the agent."""
import threading
from io import BytesIO
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from test_documents import isolated_app, pdf_bytes


class DocumentDeletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.enterContext(isolated_app())
        self.client = self.enterContext(TestClient(self.fixture.module.app))
        self.service = self.fixture.service

    def ready(self, name):
        response = self.client.post('/upload', files={'file': (name, pdf_bytes(), 'application/pdf')})
        self.assertEqual(response.status_code, 202)
        saved = response.json()['filename']
        return next(item for item in self.service.list_documents() if item['filename'] == saved)

    def test_deletes_only_requested_file_metadata_and_all_its_revision_chunks(self):
        first, other = self.ready('delete.pdf'), self.ready('keep.pdf')
        legacy = self.fixture.index_dir / 'legacy-sentinel.bin'
        legacy.write_bytes(b'preserve legacy indexes')
        original = (self.fixture.data_dir / other['filename']).read_bytes()
        self.fixture.collection.items['orphan-old-revision'] = ('old text', {
            'document_id': first['id'], 'revision': 'older', 'filename': first['filename'], 'page': 1})
        response = self.client.delete('/documents/' + first['id'])
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.fixture.data_dir / first['filename']).exists())
        self.assertNotIn(first['id'], self.service.records())
        self.assertFalse(any(meta['document_id'] == first['id'] for _, meta in self.fixture.collection.items.values()))
        self.assertEqual((self.fixture.data_dir / other['filename']).read_bytes(), original)
        self.assertEqual(legacy.read_bytes(), b'preserve legacy indexes')
        self.assertEqual(self.service.list_documents()[0]['status'], 'ready')
        self.assertEqual(self.client.delete('/documents/' + first['id']).status_code, 200)

    def test_cancelled_queued_job_cannot_restore_deleted_content(self):
        item = self.service.save_upload('queued.pdf', BytesIO(pdf_bytes()))
        self.service.queue(item['id'])
        self.service.delete_document(item['id'])
        self.service.process_document(item['id'])  # Delayed BackgroundTasks callback.
        self.assertEqual(self.service.list_documents(), [])
        self.assertNotIn(item['id'], self.service.pending)
        self.assertNotIn(item['id'], self.service.records())
        self.fixture.model.embed_documents.assert_not_called()
        with self.assertRaises(self.fixture.documents.DocumentError):
            self.service.queue(item['id'])

    def test_embedding_already_running_cannot_publish_after_delete(self):
        item = self.service.save_upload('running.pdf', BytesIO(pdf_bytes()))
        self.service.queue(item['id'])
        entered, release = threading.Event(), threading.Event()

        def embed(texts):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Test did not release the isolated embedding operation')
            return [[1.0, 1.0] for _ in texts]

        self.fixture.model.embed_documents.side_effect = embed
        worker = threading.Thread(target=self.service.process_document, args=(item['id'],), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(10), 'Isolated processing never reached embedding')
            self.service.delete_document(item['id'])
        finally:
            release.set(); worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.fixture.collection.items, {})
        self.assertNotIn(item['id'], self.service.records())
        self.assertEqual(self.service.list_documents(), [])
        self.assertFalse((self.fixture.data_dir / item['filename']).exists())

    def test_cleanup_failure_stays_unavailable_and_can_retry_after_restart(self):
        item = self.ready('retry-delete.pdf')
        with patch.object(self.fixture.collection, 'delete', side_effect=OSError('isolated deletion failure')):
            response = self.client.delete('/documents/' + item['id'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'deletion_incomplete')
        self.assertTrue((self.fixture.data_dir / item['filename']).exists())
        listed = self.service.list_documents()[0]
        self.assertEqual((listed['status'], listed['stage']), ('failed', 'deleting'))
        self.assertFalse(self.client.get('/status').json()['has_documents'])
        with self.assertRaises(self.fixture.documents.DocumentError):
            self.service.active_documents([item['id']])
        restarted = self.fixture.documents.DocumentLibrary(self.fixture.data_dir, self.fixture.index_dir)
        restarted.collection = self.service.collection
        restarted.delete_document(item['id'])
        restarted.process_document(item['id'])
        self.assertEqual(restarted.list_documents(), [])

    def test_reupload_uses_a_new_identity_and_old_callbacks_stay_cancelled(self):
        item = self.ready('notes.pdf')
        self.service.delete_document(item['id'])
        replacement = self.ready('notes.pdf')
        self.assertNotEqual(replacement['id'], item['id'])
        self.service.process_document(item['id'])
        self.assertEqual(self.service.list_documents()[0]['id'], replacement['id'])
        self.assertEqual(self.service.list_documents()[0]['status'], 'ready')

    def test_unknown_ids_and_paths_do_not_delete_other_files(self):
        item = self.ready('keep.pdf')
        before = (self.fixture.data_dir / item['filename']).read_bytes()
        for value in ('unknown', '../keep.pdf', str(self.fixture.data_dir / 'keep.pdf')):
            with self.subTest(value=value), self.assertRaises(self.fixture.documents.DocumentError):
                self.service.delete_document(value)
        self.assertEqual((self.fixture.data_dir / item['filename']).read_bytes(), before)
        self.assertEqual(self.service.deletions(), {})

    def test_foreign_origin_cannot_delete_document(self):
        item = self.ready('keep.pdf')
        response = self.client.delete('/documents/' + item['id'], headers={'Origin': 'https://foreign.example'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue((self.fixture.data_dir / item['filename']).exists())
