"""Temporary SQLite/API/context regressions. No real models, files, or sessions."""
import hashlib
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from test_documents import ROOT, isolated_app, load_module, pdf_bytes


class ConversationsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = self.enterContext(isolated_app())
        self.client = self.enterContext(TestClient(self.fixture.module.app))
        self.service = self.fixture.service
        self.fixture.workflow.invoke.return_value = {
            'messages': [AIMessage(content='A general study answer.')], 'active_documents': [],
            'citations': [], 'answer_mode': 'general'}

    def create(self, document_ids=None):
        response = self.client.post('/conversations', json={'document_ids': document_ids or []})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def send(self, conversation_id, message, document_ids=None):
        body = {'conversation_id': conversation_id, 'message': message}
        if document_ids is not None:
            body['document_ids'] = document_ids
        response = self.client.post('/chat', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ready(self, name='notes.pdf', text='The project identifier is ORBIT-314.'):
        response = self.client.post('/upload', files={'file': (name, pdf_bytes(text), 'application/pdf')})
        self.assertEqual(response.status_code, 202)
        return next(item for item in self.service.list_documents() if item['filename'] == name)

    def nodes(self, route=None):
        with patch('dotenv.load_dotenv', return_value=False):
            nodes = load_module('chatbot.nodes', ROOT / 'src/chatbot/nodes.py')
        self.enterContext(patch.object(nodes, 'get_library', return_value=self.service))
        if route:
            self.enterContext(patch.object(nodes, 'classify_question', return_value=nodes.ChatIntent(mode=route)))
        return nodes

    def grounded_result(self, source, text='The identifier is ORBIT-314 [S1].'):
        return {'messages': [AIMessage(content=text)], 'active_documents': [source], 'answer_mode': 'document',
            'citations': [{'label': 'S1', 'document_id': source['id'], 'filename': source['filename'],
                'revision': source['revision'], 'page': 1, 'excerpt': 'The project identifier is ORBIT-314.'}]}

    def test_sqlite_restores_visible_messages_and_chatbot_context_after_restart(self):
        item = self.create()
        first = self.send(item['id'], 'I am studying cellular respiration.')
        saved = self.client.get('/conversations/' + item['id']).json()
        self.assertEqual([message['role'] for message in saved['messages']], ['user', 'assistant'])
        self.assertEqual(saved['title'], 'I am studying cellular respiration.')
        self.assertTrue(all(message['id'] and message['time'] for message in saved['messages']))
        self.assertTrue(saved['created_at'] and saved['updated_at'])
        sys.modules['chatbot.conversations']._cached_store.cache_clear()  # New local store, same temporary DB.
        self.send(item['id'], 'What should I revise next?')
        messages = self.fixture.workflow.invoke.call_args.args[0]['messages']
        self.assertEqual([message.content for message in messages], [
            'I am studying cellular respiration.', first['response'], 'What should I revise next?'])
        self.assertEqual(len(self.client.get('/conversations/' + item['id']).json()['messages']), 4)

    def test_new_chat_keeps_previous_history_and_rename_is_individual(self):
        first = self.create()
        self.send(first['id'], 'Explain biology.')
        second = self.create()
        self.send(second['id'], 'Explain chemistry.')
        response = self.client.patch('/conversations/' + first['id'], json={'title': 'Biology review'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['messages']), 2)
        listing = self.client.get('/conversations').json()['conversations']
        self.assertEqual({item['id'] for item in listing}, {first['id'], second['id']})
        self.assertEqual(self.client.get('/conversations/' + second['id']).json()['title'], 'Explain chemistry.')
        self.assertEqual(self.client.patch('/conversations/' + first['id'], json={'title': '   '}).status_code, 400)

    def test_selection_is_persistent_and_only_new_ready_sources_can_be_added(self):
        source = self.ready()
        item = self.create()
        response = self.client.patch('/conversations/' + item['id'], json={'document_ids': [source['id']]})
        self.assertEqual(response.status_code, 200)
        self.send(item['id'], 'Hello')
        self.assertEqual(self.fixture.workflow.invoke.call_args.args[0]['document_ids'], [source['id']])
        self.assertEqual(self.client.get('/conversations/' + item['id']).json()['document_ids'], [source['id']])
        self.service.queue(source['id'])
        self.assertEqual(self.client.patch('/conversations/' + item['id'], json={'document_ids': []}).status_code, 200)
        self.assertEqual(self.client.patch('/conversations/' + item['id'], json={'document_ids': [source['id']]}).status_code, 409)

    def test_delete_conversation_preserves_pdfs_and_cannot_resurrect_saved_id(self):
        source = self.ready()
        item = self.create([source['id']])
        self.send(item['id'], 'Hello')
        self.assertEqual(self.client.delete('/conversations/' + item['id']).status_code, 200)
        self.assertEqual(self.client.get('/conversations/' + item['id']).status_code, 404)
        self.assertEqual(self.client.post('/chat', json={'conversation_id': item['id'], 'message': 'Again'}).status_code, 404)
        self.assertEqual(self.client.get('/conversations').json()['conversations'], [])
        self.assertTrue((self.fixture.data_dir / source['filename']).exists())
        self.assertEqual(self.service.list_documents()[0]['status'], 'ready')

    def test_browser_sessions_cannot_read_rename_delete_or_continue_each_others_chats(self):
        item = self.create()
        self.send(item['id'], 'PRIVATE_STUDY_TOPIC')
        other = self.enterContext(TestClient(self.fixture.module.app))
        self.assertEqual(other.get('/conversations').json()['conversations'], [])
        path = '/conversations/' + item['id']
        for response in (other.get(path), other.patch(path, json={'title': 'stolen'}), other.delete(path),
                         other.post('/chat', json={'conversation_id': item['id'], 'message': 'Continue'})):
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('PRIVATE_STUDY_TOPIC', response.text)
        self.assertEqual(len(self.client.get(path).json()['messages']), 2)

    def test_session_cookie_and_origin_protection(self):
        response = self.client.get('/conversations')
        cookie = response.headers['set-cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=strict', cookie)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        response = self.client.post('/conversations', json={}, headers={'Origin': 'https://foreign.example'})
        self.assertEqual(response.status_code, 403)

    def test_busy_conversation_rejects_parallel_mutations(self):
        item = self.create()
        owner = hashlib.sha256(self.client.cookies.get('study_session').encode()).hexdigest()
        with self.fixture.module.conversations().turn(owner, item['id']):
            self.assertEqual(self.client.patch('/conversations/' + item['id'], json={'title': 'racing'}).status_code, 409)
            self.assertEqual(self.client.delete('/conversations/' + item['id']).status_code, 409)
        self.assertEqual(self.client.get('/conversations/' + item['id']).json()['title'], 'New chat')

    def test_document_error_is_persisted_without_general_fallback(self):
        item = self.create()
        self.fixture.workflow.invoke.side_effect = self.fixture.documents.DocumentError('PDF embeddings unavailable.', 503, 'retrieval_failed')
        response = self.client.post('/chat', json={'conversation_id': item['id'], 'message': 'Explain my PDF.'})
        self.assertEqual(response.status_code, 503)
        messages = self.client.get('/conversations/' + item['id']).json()['messages']
        self.assertEqual([message['role'] for message in messages], ['user', 'error'])
        self.assertEqual(messages[-1]['text'], 'PDF embeddings unavailable.')

    def test_deleted_source_preserves_archived_citations_and_truthful_followup(self):
        source = self.ready()
        item = self.create([source['id']])
        self.fixture.workflow.invoke.return_value = self.grounded_result(source)
        self.send(item['id'], 'What is in notes.pdf?')
        self.assertEqual(self.client.delete('/documents/' + source['id']).status_code, 200)
        saved = self.client.get('/conversations/' + item['id']).json()
        self.assertEqual(saved['document_ids'], [])
        citation = saved['messages'][-1]['citations'][0]
        self.assertFalse(citation['available'])
        self.assertEqual(citation['availability'], 'deleted')
        self.assertEqual(citation['excerpt'], 'The project identifier is ORBIT-314.')
        nodes = self.nodes()
        self.fixture.workflow.invoke.side_effect = lambda state, config: nodes.chat_node(state)
        with patch.object(nodes, 'get_llm') as model:
            result = self.send(item['id'], 'Make bullet points', [])
        self.assertIn('deleted', result['response'])
        self.assertEqual(result['citations'], [])
        model.assert_not_called()

    def test_source_change_removes_old_document_answer_from_chatbot_evidence(self):
        first, second = self.ready('first.pdf'), self.ready('second.pdf', 'A different project.')
        item = self.create([first['id']])
        self.fixture.workflow.invoke.return_value = self.grounded_result(first, 'PRIVATE_FIRST_SOURCE [S1].')
        self.send(item['id'], 'Summarize first.pdf.')
        self.fixture.workflow.invoke.return_value = {'messages': [AIMessage(content='General reply.')],
            'active_documents': [], 'citations': [], 'answer_mode': 'general'}
        self.send(item['id'], 'Explain a general concept.', [second['id']])
        messages = self.fixture.workflow.invoke.call_args.args[0]['messages']
        self.assertNotIn('PRIVATE_FIRST_SOURCE', ' '.join(message.content for message in messages))

    def test_deletion_during_answer_does_not_publish_new_stale_document_content(self):
        source = self.ready()
        item = self.create([source['id']])

        def model_result(state, config):
            self.service.delete_document(source['id'])
            return self.grounded_result(source)

        self.fixture.workflow.invoke.side_effect = model_result
        response = self.client.post('/chat', json={'conversation_id': item['id'], 'message': 'Summarize notes.pdf.'})
        self.assertEqual(response.status_code, 404)
        messages = self.client.get('/conversations/' + item['id']).json()['messages']
        self.assertEqual([message['role'] for message in messages], ['user', 'error'])
        self.assertFalse(any(message.get('citations') for message in messages))

    def test_legacy_import_is_idempotent_and_deleted_import_does_not_reappear(self):
        body = {'legacy_id': 'old-browser-thread', 'messages': [
            {'role': 'user', 'text': 'Help me study.', 'time': 1234},
            {'role': 'assistant', 'text': 'Try active recall.', 'time': 1235}], 'document_ids': []}
        first = self.client.post('/conversations/import', json=body).json()
        second = self.client.post('/conversations/import', json=body).json()
        self.assertTrue(first['imported'])
        self.assertFalse(second['imported'])
        self.assertEqual(first['conversation_id'], second['conversation_id'])
        self.assertEqual(len(self.client.get('/conversations').json()['conversations']), 1)
        self.client.delete('/conversations/' + first['conversation_id'])
        self.client.post('/conversations/import', json=body)
        self.assertEqual(self.client.get('/conversations').json()['conversations'], [])

    def test_contextual_content_commands_use_selected_pdf_without_general_routing(self):
        source = self.ready()
        nodes = self.nodes()
        model = Mock()
        model.invoke.return_value = AIMessage(content='The project identifier is ORBIT-314 [S1].')
        with patch.object(nodes, 'classify_question') as classify, patch.object(nodes, 'get_llm', return_value=model):
            for question in ('explain all topics', 'explain all the topics', 'make bullet points', 'Make it into bullet points.'):
                with self.subTest(question=question):
                    result = nodes.chat_node({'messages': [HumanMessage(content=question)],
                                              'document_ids': [source['id']]})
                    self.assertEqual(result['answer_mode'], 'document')
                    self.assertEqual(result['citations'][0]['document_id'], source['id'])
        classify.assert_not_called()

    def test_greetings_stay_general_and_contextual_requests_keep_processing_errors(self):
        source = self.ready()
        self.service.queue(source['id'])
        nodes = self.nodes()
        with patch.object(nodes, 'get_llm') as model:
            greeting = nodes.chat_node({'messages': [HumanMessage(content='Hello')], 'document_ids': [source['id']]})
            self.assertEqual(greeting['answer_mode'], 'general')
            self.assertEqual(greeting['citations'], [])
            with self.assertRaises(self.fixture.documents.DocumentError) as caught:
                nodes.chat_node({'messages': [HumanMessage(content='explain all topics')], 'document_ids': [source['id']]})
        self.assertIn('processing', str(caught.exception))
        model.assert_not_called()

    def test_draft_chat_persists_only_when_first_message_is_sent(self):
        # On a new draft, no conversation row exists in SQLite initially
        self.assertEqual(self.client.get('/conversations').json()['conversations'], [])
        # Sending the first message from a draft (omitting conversation_id) creates the record
        response = self.client.post('/chat', json={'message': 'Explain thermodynamics.'})
        self.assertEqual(response.status_code, 200)
        cid = response.json()['conversation_id']
        self.assertTrue(cid)
        conversations = self.client.get('/conversations').json()['conversations']
        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]['id'], cid)
        self.assertEqual(conversations[0]['title'], 'Explain thermodynamics.')

