'use strict';

const el = id => document.getElementById(id);
const storage = {
    read(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
    write(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private/quota-limited storage: keep the current session usable. */ } }
};
const threadKey = 'ai_study_assistant_thread_id';
const historyKey = 'ai_study_assistant_messages_v1';
const selectionKey = 'ai_study_assistant_sources_v1';
// SQLite is authoritative for saved chats. The old storage keys remain intact
// so conversations.js can import the existing browser history once.
let threadId = null;
let chatHistory = [];
let documents = [];
const previousSelection = storage.read(selectionKey, []);
const selected = new Set(Array.isArray(previousSelection) ? previousSelection.filter(id => typeof id === 'string') : []);
let libraryAvailable = false;
let serverOnline = false;
let hasLoaded = false;
let waiting = false;
let requestId = 0;
let refreshTimer;
let serverTimer;
let lastSnapshot = '';
let noticeDocumentId = null;
const queueing = new Set();

function announce(text) { el('announcer').textContent = text; }
function sizeLabel(bytes) {
    if (!bytes) return '0 B';
    const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 4);
    return `${(bytes / 1024 ** unit).toLocaleString(undefined, { maximumFractionDigits: unit ? 1 : 0 })} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`;
}
function textElement(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
}
async function api(url, options = {}) {
    const response = await fetch(url, { cache: 'no-store', ...options });
    let data;
    try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response. Please retry.'); }
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('The server returned an invalid response. Please retry.');
    if (!response.ok || data.error) {
        const error = new Error(typeof data.error === 'string' ? data.error : 'The request could not be completed. Please retry.');
        error.data = data;
        throw error;
    }
    return data;
}
function persistSelection() {
    storage.write(selectionKey, [...selected]);
    window.StudyChats?.selectionChanged();
}
function activeDocuments() { return libraryAvailable ? documents.filter(item => selected.has(item.id) && item.status === 'ready') : []; }

function updateServiceState() {
    el('service-state').textContent = !serverOnline ? 'Server offline'
        : !libraryAvailable ? 'Online · Sources unavailable'
        : `Online · ${documents.filter(item => item.status === 'ready').length} ready`;
}
async function refreshServerStatus() {
    clearTimeout(serverTimer);
    try {
        const result = await api('/health');
        serverOnline = result.status === 'online';
    } catch { serverOnline = false; }
    updateServiceState(); updateComposer();
    serverTimer = setTimeout(refreshServerStatus, 10000);
}

function updateComposer() {
    const active = activeDocuments();
    const sendBtn = el('send-message');
    sendBtn.disabled = waiting || !serverOnline || !window.StudyChats?.ready || !el('chat-input').value.trim();
    el('chat-input').disabled = waiting;
    el('new-chat').disabled = waiting || !window.StudyChats?.ready;
    el('new-chat-shortcut').disabled = waiting || !window.StudyChats?.ready;
    if (waiting) {
        sendBtn.innerHTML = '<span class="status-spinner-send" aria-hidden="true"></span><span class="sr-only">Thinking…</span>';
        sendBtn.title = 'Thinking…';
        sendBtn.setAttribute('aria-label', 'Thinking…');
    } else {
        sendBtn.innerHTML = '<svg class="send-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg><span class="sr-only">Send</span>';
        sendBtn.title = 'Send message';
        sendBtn.setAttribute('aria-label', 'Send message');
    }
    el('composer-help').textContent = waiting ? 'Preparing an answer…'
        : !window.StudyChats?.ready ? 'Loading saved conversations…'
        : !serverOnline ? 'Waiting for the server. Your message will stay here.'
        : !libraryAvailable ? 'General chat is available. Refresh Sources to verify PDFs.'
        : !active.length ? 'General chat. Ask a study question, or select a ready PDF.'
        : `${active.length} ${active.length === 1 ? 'PDF' : 'PDFs'} selected for document questions.`;
    const bar = el('active-sources');
    bar.replaceChildren(textElement('span', 'Sources:', 'active-label'));
    if (!active.length) bar.append(textElement('span', 'General chat', 'active-empty'));
    active.forEach(item => {
        const chip = textElement('button', item.filename, 'source-chip');
        chip.type = 'button';
        chip.disabled = waiting;
        chip.title = `Remove ${item.filename} from active sources`;
        chip.setAttribute('aria-label', chip.title);
        chip.addEventListener('click', () => { selected.delete(item.id); persistSelection(); renderSources(); });
        bar.append(chip);
    });
}

function renderSources() {
    const list = el('sources-list');
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    const focusId = focused?.dataset.documentId;
    const focusAction = focused?.dataset.action;
    const query = el('source-search').value.trim().toLocaleLowerCase();
    const visible = documents.filter(item => item.filename.toLocaleLowerCase().includes(query));
    const fragment = document.createDocumentFragment();
    visible.forEach(item => {
        const row = document.createElement('li');
        row.className = `source-row${selected.has(item.id) ? ' selected' : ''}`;
        const label = document.createElement('label');
        label.className = 'source-label';
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = selected.has(item.id);
        check.disabled = waiting || !libraryAvailable || (item.status !== 'ready' && !selected.has(item.id));
        check.dataset.documentId = item.id;
        check.dataset.action = 'select';
        check.setAttribute('aria-label', `Use ${item.filename} as a source`);
        check.addEventListener('change', () => {
            if (check.checked && selected.size >= 20) { check.checked = false; announce('Select up to 20 sources at a time.'); return; }
            if (check.checked) selected.add(item.id); else selected.delete(item.id);
            persistSelection(); renderSources();
        });
        const name = textElement('span', item.filename, 'source-name');
        name.dir = 'auto';
        label.append(check, name);
        const heading = document.createElement('div');
        heading.className = 'source-heading-row';
        heading.append(label, window.documentActions(item, {
            disabled: waiting || queueing.has(item.id) || !libraryAvailable,
            remove: deleteSource
        }));
        row.append(heading);
        const meta = document.createElement('div');
        meta.className = 'source-meta';
        meta.append(textElement('span', sizeLabel(item.size_bytes)));
        if (item.page_count) meta.append(textElement('span', `${item.page_count} pages`));
        const states = { saved: 'Saved', processing: 'Processing', ready: 'Ready', failed: 'Failed' };
        const stageLabels = {
            queued: 'Queued',
            extracting: 'Extracting pages',
            chunking: 'Splitting text',
            embedding: 'Indexing chunks',
            verifying: 'Verifying index',
            deleting: 'Deleting…',
        };
        const status = document.createElement('span');
        status.className = `source-status ${libraryAvailable ? item.status : ''}`;
        if (libraryAvailable && item.status === 'processing') {
            const spinner = document.createElement('span');
            spinner.className = 'status-spinner';
            spinner.setAttribute('aria-hidden', 'true');
            const text = document.createElement('span');
            text.className = 'status-label';
            const currentLabel = stageLabels[item.stage] || 'Processing…';
            text.textContent = currentLabel;
            status.title = `Status: ${currentLabel}`;
            status.append(spinner, text);
        } else {
            status.textContent = libraryAvailable ? states[item.status] : 'Unverified';
        }
        meta.append(status);
        row.append(meta);
        if (item.error || item.warning) row.append(textElement('p', item.error || item.warning, `source-note${item.error ? ' error' : ''}`));
        if ((item.status === 'saved' || item.status === 'failed') && item.stage !== 'deleting') {
            const button = textElement('button', queueing.has(item.id) ? 'Queuing…' : item.status === 'failed' ? 'Retry processing' : 'Process PDF', 'source-action');
            button.type = 'button';
            button.dataset.documentId = item.id;
            button.dataset.action = 'process';
            button.disabled = queueing.has(item.id) || !libraryAvailable;
            button.addEventListener('click', () => processSource(item.id));
            row.append(button);
        }
        fragment.append(row);
    });
    if (!visible.length) fragment.append(textElement('li', query ? 'No documents match your search.' : 'No PDFs yet. Chat now, or add a PDF for document questions.', 'no-results'));
    list.replaceChildren(fragment);
    el('source-count').textContent = String(documents.length);
    if (focusId) {
        const target = [...list.querySelectorAll('[data-document-id]')].find(node => node.dataset.documentId === focusId && node.dataset.action === focusAction && !node.disabled)
            || [...list.querySelectorAll('input')].find(node => node.dataset.documentId === focusId && !node.disabled);
        (target || el('source-search')).focus({ preventScroll: true });
    }
    updateComposer();
}

async function deleteSource(item) {
    queueing.add(item.id); renderSources();
    try {
        const result = await api(`/documents/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        if (!result.deleted || result.document_id !== item.id) throw new Error('Deletion was not confirmed. Refresh Sources.');
        ++requestId; clearTimeout(refreshTimer);
        documents = documents.filter(source => source.id !== item.id);
        selected.delete(item.id); persistSelection();
        if (noticeDocumentId === item.id) noticeDocumentId = null;
        renderSources(); updateServiceState();
        await window.StudyChats?.sourceDeleted(item.id);
        uploadMessage(`${item.filename} deleted. Earlier conversation references are unavailable.`, 'success');
        announce(`${item.filename} deleted.`);
        el('source-search').focus();
    } catch (error) { uploadMessage(error.message, 'error'); announce(error.message); }
    finally { queueing.delete(item.id); await refreshDocuments(); }
}

function uploadMessage(text, state = '') {
    const node = el('upload-message');
    if (node.textContent === text && node.className === `upload-message ${state}`) return;
    node.textContent = text;
    node.hidden = !text;
    node.className = `upload-message ${state}`;
}
async function refreshDocuments() {
    clearTimeout(refreshTimer);
    const current = ++requestId;
    el('refresh-sources').disabled = true;
    el('sources-list').setAttribute('aria-busy', 'true');
    if (!hasLoaded) el('library-message').textContent = 'Loading sources…';
    try {
        const result = await api('/documents');
        if (!Array.isArray(result.documents) || !result.documents.every(item => item && /^[a-f0-9]{32}$/.test(item.id)
            && typeof item.filename === 'string' && Number.isSafeInteger(item.size_bytes) && item.size_bytes >= 0
            && ['saved', 'processing', 'ready', 'failed'].includes(item.status))) throw new Error('The document list is invalid. Please retry.');
        if (current !== requestId) return;
        const oldAvailable = libraryAvailable;
        documents = result.documents;
        libraryAvailable = true; serverOnline = true; hasLoaded = true;
        // Keep an existing selection if processing/reverification changes its
        // readiness, so document questions receive its actual status. It may
        // be deselected, but only ready PDFs can be newly selected.
        const available = new Set(documents.map(item => item.id));
        for (const id of selected) if (!available.has(id)) selected.delete(id);
        persistSelection();
        const snapshot = JSON.stringify(documents);
        if (snapshot !== lastSnapshot || !oldAvailable) { lastSnapshot = snapshot; renderSources(); }
        updateServiceState(); updateComposer();
        el('library-message').textContent = '';
        el('library-message').hidden = true;
        el('retry-sources').hidden = true;
        if (noticeDocumentId) {
            const uploaded = documents.find(item => item.id === noticeDocumentId);
            if (uploaded?.status === 'ready') uploadMessage(`${uploaded.filename} is indexed. Select it to use in chat.`, 'success');
            else if (uploaded?.status === 'failed') uploadMessage(`${uploaded.filename} was saved, but processing failed. Use Retry processing below.`, 'error');
            else if (uploaded) uploadMessage(`${uploaded.filename} saved. ${uploaded.status === 'processing' ? 'Processing is in progress.' : 'Use Process PDF before asking about its contents.'}`);
            else { uploadMessage(''); noticeDocumentId = null; }
        }
    } catch (error) {
        if (current !== requestId) return;
        libraryAvailable = false;
        updateServiceState();
        el('library-message').textContent = `Could not refresh sources. ${error.message}`;
        el('library-message').className = 'library-message error';
        el('library-message').hidden = false;
        el('retry-sources').hidden = false;
        if (noticeDocumentId) uploadMessage('Your PDF was saved, but readiness could not be verified. Refresh Sources.', 'error');
        renderSources();
    } finally {
        if (current === requestId) {
            el('refresh-sources').disabled = false;
            el('sources-list').setAttribute('aria-busy', 'false');
            refreshTimer = setTimeout(refreshDocuments, documents.some(item => item.status === 'processing') ? 2000 : 15000);
        }
    }
}

async function processSource(id) {
    noticeDocumentId = null;
    queueing.add(id); renderSources();
    try {
        await api(`/documents/${encodeURIComponent(id)}/process`, { method: 'POST' });
        noticeDocumentId = id;
        uploadMessage('Processing queued. Readiness will update after the index is verified.');
        announce('Processing queued. Readiness will update when indexing is verified.');
    } catch (error) { uploadMessage(error.message, 'error'); }
    finally { queueing.delete(id); await refreshDocuments(); renderSources(); }
}
el('add-pdf').addEventListener('click', () => el('file-input').click());
el('file-input').addEventListener('change', async () => {
    const file = el('file-input').files[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.pdf')) { uploadMessage('Choose a PDF file.', 'error'); el('file-input').value = ''; return; }
    el('add-pdf').disabled = true;
    noticeDocumentId = null;
    uploadMessage(`Saving ${file.name}…`);
    const form = new FormData(); form.append('file', file);
    try {
        const result = await api('/upload', { method: 'POST', body: form });
        if (!result.saved || !result.document || typeof result.document.id !== 'string') throw new Error('Saving was not confirmed. Refresh Sources before retrying the upload.');
        noticeDocumentId = result.document.id;
        uploadMessage(`${result.filename} saved. Processing must finish before chat can use it.`);
    } catch (error) { uploadMessage(error.message, 'error'); }
    finally { el('file-input').value = ''; el('add-pdf').disabled = false; await refreshDocuments(); }
});
el('refresh-sources').addEventListener('click', refreshDocuments);
el('retry-sources').addEventListener('click', refreshDocuments);
el('source-search').addEventListener('input', renderSources);

function markdownContent(text) {
    if (!window.marked) { const fallback = textElement('div', text); fallback.style.whiteSpace = 'pre-wrap'; return fallback; }
    // Parse into an inert template, then rebuild only allowed nodes/attributes.
    // No model-generated HTML is ever inserted into the active document directly.
    const template = document.createElement('template');
    template.innerHTML = window.marked.parse(text);
    const allowed = new Set(['P', 'STRONG', 'EM', 'UL', 'OL', 'LI', 'CODE', 'PRE', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4', 'BR', 'A']);
    function clean(node) {
        if (node.nodeType === Node.TEXT_NODE) return document.createTextNode(node.textContent);
        if (node.nodeType !== Node.ELEMENT_NODE) return document.createDocumentFragment();
        if (['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'SVG', 'MATH', 'IMG'].includes(node.tagName)) return document.createDocumentFragment();
        const copy = allowed.has(node.tagName) ? document.createElement(node.tagName.toLowerCase()) : document.createDocumentFragment();
        if (node.tagName === 'A') {
            const href = node.getAttribute('href') || '';
            if (/^https?:\/\//i.test(href)) { copy.setAttribute('href', href); copy.setAttribute('rel', 'noreferrer noopener'); }
        }
        node.childNodes.forEach(child => copy.append(clean(child)));
        return copy;
    }
    const result = document.createDocumentFragment();
    template.content.childNodes.forEach(node => result.append(clean(node)));
    return result;
}
function appendMessage(item) {
    el('welcome')?.remove();
    const article = document.createElement('article'); article.className = `message ${item.role}`;
    const heading = document.createElement('div'); heading.className = 'message-heading';
    heading.append(textElement('strong', item.role === 'user' ? 'You' : item.role === 'error' ? 'Request failed' : 'Assistant'));
    const knownTime = Number.isFinite(item.time) && item.time > 0;
    const time = textElement('time', knownTime
        ? new Date(item.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Imported message');
    if (knownTime) time.dateTime = new Date(item.time).toISOString();
    heading.append(time); article.append(heading);
    const body = document.createElement('div'); body.className = `message-body${item.role === 'error' ? ' error' : ''}`;
    if (item.role === 'assistant') body.append(markdownContent(item.text)); else body.textContent = item.text;
    article.append(body);
    if (item.unavailable_source_ids?.length) article.append(textElement('p', 'An associated source is unavailable. This is a preserved conversation message.', 'source-unavailable'));
    if (Array.isArray(item.sources) && item.sources.length) article.append(textElement('p', `Answered from: ${item.sources.filter(name => typeof name === 'string').join(', ')}`, 'answer-sources'));
    const citations = Array.isArray(item.citations) ? item.citations.filter(cite => cite && typeof cite.filename === 'string' && Number.isInteger(cite.page) && cite.page > 0 && typeof cite.excerpt === 'string') : [];
    if (citations.length) {
        const details = document.createElement('details'); details.className = 'citations';
        details.append(textElement('summary', `Source passages (${citations.length})`));
        citations.forEach(cite => {
            const source = document.createElement('div'); source.className = 'citation';
            source.append(textElement('p', `[${cite.label}] ${cite.filename} · page ${cite.page}`, 'citation-name'), textElement('blockquote', cite.excerpt));
            if (cite.available === false) source.append(textElement('p', cite.availability === 'archived'
                ? 'Archived reference; this passage has not been reverified.'
                : `Source unavailable (${cite.availability || 'unverified'}). This passage is an archived reference.`, 'source-unavailable'));
            details.append(source);
        });
        article.append(details);
    }
    el('messages').append(article);
}
function recordMessage(item) {
    chatHistory.push(item);
    appendMessage(item); el('conversation').scrollTop = el('conversation').scrollHeight;
}
chatHistory.forEach(appendMessage);
el('conversation').scrollTop = el('conversation').scrollHeight;
function resizeComposer() {
    const input = el('chat-input');
    if (!input) return;
    input.style.height = 'auto';
    const maxHeight = 180;
    const scrollHeight = input.scrollHeight;
    if (scrollHeight > maxHeight) {
        input.style.height = `${maxHeight}px`;
        input.style.overflowY = 'auto';
    } else {
        input.style.height = `${Math.max(scrollHeight, 44)}px`;
        input.style.overflowY = 'hidden';
    }
}

function resetComposer() {
    const input = el('chat-input');
    if (!input) return;
    input.value = '';
    input.style.height = '44px';
    input.style.overflowY = 'hidden';
    updateComposer();
}

window.resizeComposer = resizeComposer;
window.resetComposer = resetComposer;

el('chat-form').addEventListener('submit', async event => {
    event.preventDefault();
    const question = el('chat-input').value.trim();
    if (!question || waiting || !serverOnline || !window.StudyChats?.ready) return;
    await window.StudyChats.send(question);
});
el('chat-input').addEventListener('input', () => {
    resizeComposer();
    updateComposer();
});
el('chat-input').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); el('chat-form').requestSubmit(); }
});

const mobile = window.matchMedia('(max-width: 760px)');
function setDrawer(open) {
    const sidebar = el('sources-sidebar');
    const modal = open && mobile.matches;
    sidebar.classList.toggle('is-open', modal);
    el('drawer-backdrop').hidden = !modal;
    el('open-sources').setAttribute('aria-expanded', String(modal));
    el('chat-workspace').inert = modal;
    if (modal) {
        sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true'); el('close-sources').focus();
    } else {
        sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal');
        if (mobile.matches) el('open-sources').focus();
    }
}
el('open-sources').addEventListener('click', () => setDrawer(true));
el('close-sources').addEventListener('click', () => setDrawer(false));
el('drawer-backdrop').addEventListener('click', () => setDrawer(false));
mobile.addEventListener('change', () => setDrawer(false));
el('sources-sidebar').addEventListener('keydown', event => {
    if (!mobile.matches || !el('sources-sidebar').classList.contains('is-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); setDrawer(false); }
    if (event.key === 'Tab') {
        const controls = [...el('sources-sidebar').querySelectorAll('button:not(:disabled), input:not(:disabled), summary')].filter(node => !node.hidden && node.tabIndex !== -1 && node.getClientRects().length);
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
});
async function loadModelName() {
    try { const result = await api('/status'); el('model-name').textContent = result.model || ''; }
    catch { /* Source polling reports the actionable service error. */ }
}
// Delegate clicks on prompt starter chips to populate composer
document.addEventListener('click', event => {
    const chip = event.target.closest('.prompt-chip');
    if (!chip) return;
    const prompt = chip.dataset.prompt;
    if (!prompt) return;
    const input = el('chat-input');
    if (!input) return;
    input.value = prompt;
    resizeComposer();
    updateComposer();
    input.focus();
});

renderSources(); refreshDocuments(); refreshServerStatus(); loadModelName(); resetComposer();
