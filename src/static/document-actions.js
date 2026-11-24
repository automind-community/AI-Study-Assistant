'use strict';

// Shared icons for menus and dialogs
window.createDotsIcon = function () {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('fill', 'currentColor');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'menu-icon-dots');
    svg.innerHTML = '<circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="13" cy="8" r="1.5"/>';
    return svg;
};

window.createPencilIcon = function () {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'action-btn-icon');
    svg.innerHTML = '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/>';
    return svg;
};

window.createTrashIcon = function () {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'action-btn-icon');
    svg.innerHTML = '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>';
    return svg;
};

// Global click-outside listener to close any open .item-menu
document.addEventListener('click', event => {
    document.querySelectorAll('.item-menu[open]').forEach(menu => {
        if (!menu.contains(event.target)) {
            menu.removeAttribute('open');
        }
    });
});

// Shared native dialog keeps confirmation, keyboard focus and long names usable.
window.workspaceDialog = function ({ title, description, confirmLabel, value = null }) {
    const dialog = document.getElementById('action-dialog');
    if (dialog.open) return Promise.resolve(null);
    const form = document.getElementById('action-dialog-form');
    const input = document.getElementById('action-value');
    const errorEl = document.getElementById('action-error');
    const cancelBtn = document.getElementById('action-cancel');
    const confirmBtn = document.getElementById('action-confirm');

    document.getElementById('action-title').textContent = title;
    document.getElementById('action-description').textContent = description;
    confirmBtn.textContent = confirmLabel;
    document.getElementById('action-value-label').hidden = value === null;
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.hidden = true;
    }
    input.required = value !== null;
    input.value = value ?? '';
    dialog.returnValue = '';
    const trigger = document.activeElement;

    return new Promise(resolve => {
        let cleanup = () => {};

        const onSubmit = event => {
            if (value !== null) {
                const trimmed = input.value.trim();
                if (!trimmed) {
                    event.preventDefault();
                    if (errorEl) {
                        errorEl.textContent = 'Please enter a title.';
                        errorEl.hidden = false;
                    }
                    input.focus();
                    return;
                }
            }
            dialog.returnValue = 'confirm';
        };

        const onCancelClick = () => {
            dialog.returnValue = 'cancel';
            dialog.close('cancel');
        };

        const onInput = () => {
            if (errorEl && !errorEl.hidden && input.value.trim()) {
                errorEl.textContent = '';
                errorEl.hidden = true;
            }
        };

        const onKeyDown = event => {
            if (event.key === 'Escape') {
                event.preventDefault();
                dialog.returnValue = 'cancel';
                dialog.close('cancel');
            } else if (event.key === 'Enter' && value !== null) {
                event.preventDefault();
                const trimmed = input.value.trim();
                if (!trimmed) {
                    if (errorEl) {
                        errorEl.textContent = 'Please enter a title.';
                        errorEl.hidden = false;
                    }
                    input.focus();
                    return;
                }
                dialog.returnValue = 'confirm';
                dialog.close('confirm');
            }
        };

        form.addEventListener('submit', onSubmit);
        cancelBtn.addEventListener('click', onCancelClick);
        input.addEventListener('input', onInput);
        dialog.addEventListener('keydown', onKeyDown);

        cleanup = () => {
            form.removeEventListener('submit', onSubmit);
            cancelBtn.removeEventListener('click', onCancelClick);
            input.removeEventListener('input', onInput);
            dialog.removeEventListener('keydown', onKeyDown);
        };

        dialog.addEventListener('close', () => {
            cleanup();
            const accepted = dialog.returnValue === 'confirm';
            resolve(accepted ? (value === null ? true : input.value.trim()) : null);
            if (trigger?.isConnected) trigger.focus({ preventScroll: true });
        }, { once: true });

        dialog.showModal();
        if (value === null) {
            cancelBtn.focus();
        } else {
            input.focus();
            input.select();
        }
    });
};

window.documentActions = function (item, { disabled, remove }) {
    const menu = document.createElement('details');
    menu.className = 'item-menu';
    const trigger = document.createElement('summary');
    trigger.className = 'icon-button menu-trigger';
    trigger.setAttribute('aria-label', `Document actions for ${item.filename}`);
    trigger.setAttribute('title', `Document actions for ${item.filename}`);
    trigger.setAttribute('aria-disabled', String(disabled));
    trigger.dataset.documentId = item.id;
    trigger.dataset.action = 'menu';
    trigger.append(window.createDotsIcon());
    trigger.addEventListener('click', event => { if (disabled) event.preventDefault(); });

    const panel = document.createElement('div');
    panel.className = 'item-menu-actions';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'danger-action menu-action-btn';
    button.disabled = disabled;
    button.dataset.documentId = item.id;
    button.dataset.action = 'delete';

    const labelSpan = document.createElement('span');
    labelSpan.textContent = 'Delete document';
    button.append(window.createTrashIcon(), labelSpan);

    button.addEventListener('click', async () => {
        menu.open = false;
        trigger.focus();
        const confirmed = await window.workspaceDialog({
            title: 'Delete document?',
            description: `Delete “${item.filename}” and its search index? Conversations will keep their history and show this source as unavailable.`,
            confirmLabel: 'Delete document'
        });
        if (confirmed) await remove(item);
    });

    menu.addEventListener('keydown', event => {
        if (event.key === 'Escape' && menu.open) {
            event.preventDefault();
            event.stopPropagation();
            menu.open = false;
            trigger.focus();
        }
    });

    panel.append(button);
    menu.append(trigger, panel);
    return menu;
};
