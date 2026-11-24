'use strict';

(() => {
    const activeKey = 'study_active_conversation_v2';
    const importKey = 'study_legacy_chat_imported_v2';
    let saved = [];
    let lastSelection = '';
    let selectionWrite = Promise.resolve();

    function notice(text = '') {
        el('chats-message').textContent = text;
        el('chats-message').hidden = !text;
    }
    function rememberActive() {
        try {
            if (threadId) sessionStorage.setItem(activeKey, threadId);
            else sessionStorage.setItem(activeKey, '__draft__');
        } catch { /* SQLite still retains the conversation. */ }
    }
    function showTab(name, focus = false) {
        for (const tab of ['chats', 'sources']) {
            const active = tab === name;
            el(`tab-${tab}`).setAttribute('aria-selected', String(active));
            el(`tab-${tab}`).tabIndex = active ? 0 : -1;
            el(`${tab}-panel`).hidden = !active;
        }
        if (focus) el(`tab-${name}`).focus();
    }
    for (const name of ['chats', 'sources']) {
        el(`tab-${name}`).addEventListener('click', () => showTab(name));
        el(`tab-${name}`).addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            showTab(event.key === 'Home' ? 'chats' : event.key === 'End' ? 'sources' : name === 'chats' ? 'sources' : 'chats', true);
        });
    }

    function renderMessages() {
        el('messages').replaceChildren();
        if (!chatHistory.length) {
            const welcome = document.createElement('div');
            welcome.className = 'welcome';
            welcome.id = 'welcome';

            const header = document.createElement('div');
            header.className = 'welcome-header';

            const badge = document.createElement('div');
            badge.className = 'welcome-badge';
            const sparkle = document.createElement('span');
            sparkle.className = 'welcome-sparkle';
            sparkle.setAttribute('aria-hidden', 'true');
            sparkle.textContent = '✨';
            const badgeText = document.createElement('span');
            badgeText.textContent = 'AI Study Companion';
            badge.append(sparkle, badgeText);

            const h2 = document.createElement('h2');
            h2.textContent = 'What are you studying?';

            const p = document.createElement('p');
            p.textContent = 'Ask a conceptual question, summarize a topic, or ground your learning in verified PDF course materials with exact page citations.';

            header.append(badge, h2, p);

            const starters = document.createElement('div');
            starters.className = 'prompt-starters';
            starters.setAttribute('aria-label', 'Suggested prompts');

            const prompts = [
                { icon: '💡', text: 'Explain a tough concept simply' },
                { icon: '📄', text: 'Summarize all topics in my PDF' },
                { icon: '📝', text: 'Create 5 practice quiz questions' }
            ];

            prompts.forEach(({ icon, text }) => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'prompt-chip';
                chip.dataset.prompt = text;

                const iconSpan = document.createElement('span');
                iconSpan.className = 'chip-icon';
                iconSpan.setAttribute('aria-hidden', 'true');
                iconSpan.textContent = icon;

                const textSpan = document.createElement('span');
                textSpan.className = 'chip-text';
                textSpan.textContent = text;

                chip.append(iconSpan, textSpan);
                starters.append(chip);
            });

            welcome.append(header, starters);
            el('messages').append(welcome);
        } else chatHistory.forEach(appendMessage);
        el('conversation').scrollTop = el('conversation').scrollHeight;
    }
    function apply(item) {
        if (!item || typeof item.id !== 'string' || !Array.isArray(item.messages) || !Array.isArray(item.document_ids)) {
            throw new Error('The saved conversation could not be restored. Refresh Chats and retry.');
        }
        threadId = item.id; rememberActive();
        chatHistory = item.messages;
        selected.clear(); item.document_ids.forEach(id => selected.add(id));
        lastSelection = JSON.stringify([...selected].sort());
        persistSelection();
        el('current-chat-title').textContent = item.title;
        renderMessages(); renderSources(); render();
    }
    function selectionChanged() {
        if (!window.StudyChats.ready || !threadId) return;
        const ids = [...selected];
        const signature = JSON.stringify(ids.slice().sort());
        if (signature === lastSelection) return;
        lastSelection = signature;
        const id = threadId;
        selectionWrite = selectionWrite.then(() => api(`/conversations/${encodeURIComponent(id)}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ document_ids: ids })
        })).catch(error => { notice(`Source selection could not be saved. ${error.message}`); });
    }
    async function refresh() {
        const result = await api('/conversations');
        if (!Array.isArray(result.conversations)) throw new Error('The conversation list is invalid. Refresh Chats.');
        saved = result.conversations;
        notice(); render();
    }
    function actionMenu(item) {
        const menu = document.createElement('details'); menu.className = 'item-menu';
        const summary = document.createElement('summary');
        summary.className = 'icon-button menu-trigger';
        summary.setAttribute('aria-label', `Actions for conversation ${item.title}`);
        summary.setAttribute('title', `Actions for conversation ${item.title}`);
        summary.append(window.createDotsIcon ? window.createDotsIcon() : textElement('span', '•••'));

        const panel = document.createElement('div');
        panel.className = 'item-menu-actions';

        const renameBtn = document.createElement('button');
        renameBtn.type = 'button';
        renameBtn.className = 'menu-action-btn';
        renameBtn.disabled = waiting || !window.StudyChats.ready;
        const renameSpan = document.createElement('span');
        renameSpan.textContent = 'Rename chat';
        renameBtn.append(window.createPencilIcon ? window.createPencilIcon() : textElement('span', '✎'), renameSpan);
        renameBtn.addEventListener('click', () => { menu.open = false; summary.focus(); rename(item); });

        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'danger-action menu-action-btn';
        deleteBtn.disabled = waiting || !window.StudyChats.ready;
        const deleteSpan = document.createElement('span');
        deleteSpan.textContent = 'Delete chat';
        deleteBtn.append(window.createTrashIcon ? window.createTrashIcon() : textElement('span', '🗑'), deleteSpan);
        deleteBtn.addEventListener('click', () => { menu.open = false; summary.focus(); remove(item); });

        menu.addEventListener('keydown', event => {
            if (event.key === 'Escape' && menu.open) {
                event.preventDefault();
                event.stopPropagation();
                menu.open = false;
                summary.focus();
            }
        });

        panel.append(renameBtn, deleteBtn);
        menu.append(summary, panel);
        return menu;
    }
    function render() {
        const query = el('chat-search').value.trim().toLocaleLowerCase();
        const visible = saved.filter(item => item.title.toLocaleLowerCase().includes(query));
        const fragment = document.createDocumentFragment();
        visible.forEach(item => {
            const row = textElement('li', '', `chat-row${threadId === item.id ? ' current' : ''}`);
            const heading = textElement('div', '', 'chat-row-main');
            const button = textElement('button', item.title, 'chat-open'); button.type = 'button';
            button.disabled = waiting || !window.StudyChats.ready; button.title = item.title;
            if (threadId === item.id) button.setAttribute('aria-current', 'true');
            button.addEventListener('click', () => open(item.id));
            const time = textElement('time', new Date(item.updated_at).toLocaleString([], {
                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
            }));
            time.dateTime = item.updated_at;
            heading.append(button, actionMenu(item)); row.append(heading, time); fragment.append(row);
        });
        if (!visible.length) fragment.append(textElement('li', query ? 'No conversations match your search.' : 'No saved conversations yet. Start a new chat.', 'no-results'));
        el('chats-list').replaceChildren(fragment);
    }
    async function open(id) {
        if (waiting || !window.StudyChats.ready) return;
        waiting = true; updateComposer(); render();
        try {
            await selectionWrite;
            apply(await api(`/conversations/${encodeURIComponent(id)}`));
            if (window.resetComposer) window.resetComposer();
            else { el('chat-input').value = ''; el('chat-input').style.height = 'auto'; }
            setDrawer(false);
        } catch (error) { notice(error.message); announce(error.message); }
        finally { waiting = false; renderSources(); render(); el('chat-input').focus(); }
    }
    async function create() {
        if (waiting || !window.StudyChats.ready) return;
        // If already on an empty draft, avoid duplicate operations and simply focus the composer
        if (!threadId && chatHistory.length === 0) {
            setDrawer(false);
            if (window.resetComposer) window.resetComposer();
            else { el('chat-input').value = ''; el('chat-input').style.height = 'auto'; }
            el('chat-input').focus();
            return;
        }
        waiting = true; updateComposer(); render();
        try {
            await selectionWrite;
            // Switch to local unsaved draft state: no database record created until first message is sent
            threadId = null;
            chatHistory = [];
            selected.clear();
            lastSelection = '[]';
            rememberActive();
            persistSelection();
            el('current-chat-title').textContent = 'Study chat';
            renderMessages();
            renderSources();
            if (window.resetComposer) window.resetComposer();
            else { el('chat-input').value = ''; el('chat-input').style.height = 'auto'; }
            setDrawer(false);
        } catch (error) { notice(error.message); announce(error.message); }
        finally { waiting = false; renderSources(); render(); el('chat-input').focus(); }
    }
    async function rename(item) {
        const title = await window.workspaceDialog({ title: 'Rename chat',
            description: `Choose a title for “${item.title}”.`, confirmLabel: 'Save title', value: item.title });
        if (title === null) return;
        if (!title) { notice('Enter a chat title.'); return; }
        try {
            await api(`/conversations/${encodeURIComponent(item.id)}`, { method: 'PATCH',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) });
            if (threadId === item.id) el('current-chat-title').textContent = title;
            await refresh(); el('chat-search').focus();
        } catch (error) { notice(error.message); announce(error.message); }
    }
    async function remove(item) {
        const confirmed = await window.workspaceDialog({ title: 'Delete chat?',
            description: `Delete “${item.title}” and its messages? Its PDFs will remain in Sources.`, confirmLabel: 'Delete chat' });
        if (!confirmed) return;
        try {
            await selectionWrite;
            await api(`/conversations/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
            if (threadId === item.id) {
                threadId = null; chatHistory = []; selected.clear(); lastSelection = '[]';
                rememberActive(); persistSelection(); renderMessages(); renderSources();
                el('current-chat-title').textContent = 'Study chat';
            }
            await refresh(); announce('Chat deleted. Its PDFs remain available.');
            el('chat-search').focus();
        } catch (error) { notice(error.message); announce(error.message); }
    }
    async function send(question) {
        waiting = true; renderSources(); render();
        let recorded = false;
        try {
            await selectionWrite;
            // Persist to SQLite only when the first message is sent
            if (!threadId) {
                const item = await api('/conversations', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ document_ids: [...selected] })
                });
                threadId = item.id;
                rememberActive();
            }
            recordMessage({ role: 'user', text: question, time: Date.now() }); recorded = true;
            if (window.resetComposer) window.resetComposer();
            else { el('chat-input').value = ''; el('chat-input').style.height = 'auto'; }
            const data = await api('/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: question, conversation_id: threadId, document_ids: [...selected] }) });
            if (!Array.isArray(data.citations)) throw new Error('The answer could not be restored. Refresh Chats and reopen this conversation.');
            apply(data.conversation);
            announce(data.citations.length ? 'Answer received. Source passages are available below the answer.' : 'Answer received.');
        } catch (error) {
            if (recorded && threadId) {
                try { apply(await api(`/conversations/${encodeURIComponent(threadId)}`)); }
                catch { /* Preserve the visible request if the server cannot be reached. */ }
            }
            if (chatHistory.at(-1)?.role !== 'error' || chatHistory.at(-1)?.text !== error.message) {
                recordMessage({ role: 'error', text: error.message, time: Date.now() });
            }
            if (!recorded || !chatHistory.some(item => item.role === 'user' && item.text === question)) {
                el('chat-input').value = question;
                if (window.resizeComposer) window.resizeComposer();
            }
            announce(error.message);
        } finally {
            await refresh().catch(error => notice(`Could not refresh saved chats. ${error.message}`));
            waiting = false; renderSources(); render(); el('chat-input').focus();
        }
    }
    async function sourceDeleted(id) {
        chatHistory.forEach(message => {
            if (message.source_ids?.includes(id)) message.unavailable_source_ids = [...new Set([...(message.unavailable_source_ids || []), id])];
            message.citations?.forEach(citation => {
                if (citation.document_id === id) { citation.available = false; citation.availability = 'deleted'; }
            });
        });
        renderMessages();
        await selectionWrite;
        if (threadId) {
            try { apply(await api(`/conversations/${encodeURIComponent(threadId)}`)); }
            catch (error) { notice(`Document deleted; saved chat could not refresh. ${error.message}`); }
        }
    }

    window.StudyChats = { ready: false, send, selectionChanged, sourceDeleted };
    el('new-chat').addEventListener('click', create);
    el('new-chat-shortcut').addEventListener('click', create);
    el('refresh-chats').addEventListener('click', () => refresh().catch(error => notice(error.message)));
    el('chat-search').addEventListener('input', render);

    async function initialize() {
        try {
            await refresh(); // Establish the session cookie before other writes.
            const legacy = storage.read(historyKey, []);
            let importedId = null;
            if (!storage.read(importKey, false) && Array.isArray(legacy) && legacy.length) {
                let legacyId;
                try { legacyId = localStorage.getItem(threadKey); } catch { /* Use a stable fallback. */ }
                const messages = legacy.filter(item => item && ['user', 'assistant', 'error'].includes(item.role) && typeof item.text === 'string')
                    .slice(-1000).map(item => ({ role: item.role, text: item.text, time: Number.isFinite(item.time) && item.time >= 0 ? Math.trunc(item.time) : 0,
                        citations: Array.isArray(item.citations) ? item.citations : [], sources: Array.isArray(item.sources) ? item.sources : [] }));
                const result = await api('/conversations/import', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ legacy_id: legacyId || 'browser-backup-v1', messages, document_ids: [...selected].slice(0, 20) }) });
                storage.write(importKey, true); importedId = result.conversation_id;
                await refresh();
            }
            let active;
            try { active = sessionStorage.getItem(activeKey); } catch { /* Most recent chat fallback. */ }
            if (active === '__draft__') {
                threadId = null; chatHistory = []; selected.clear(); lastSelection = '[]'; persistSelection(); renderMessages();
                el('current-chat-title').textContent = 'Study chat';
            } else {
                const id = [active, importedId, saved[0]?.id].find(candidate => saved.some(item => item.id === candidate));
                if (id) apply(await api(`/conversations/${encodeURIComponent(id)}`));
                else { threadId = null; chatHistory = []; selected.clear(); lastSelection = '[]'; persistSelection(); renderMessages(); el('current-chat-title').textContent = 'Study chat'; }
            }
        } catch (error) { notice(`Could not load saved chats. ${error.message}`); }
        finally { window.StudyChats.ready = true; updateComposer(); render(); }
    }
    showTab('chats'); initialize();
})();
