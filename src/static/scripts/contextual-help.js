/* Contextual disclosures share the existing global bulb and native details. */
(() => {
    // Keep only weak initialization markers. Registry refresh replaces its DOM;
    // all global operations must query the currently connected disclosures.
    const initialized = new WeakSet();
    const items = () => [...document.querySelectorAll('.context-help')];
    let suppressFocus = false;
    const enabled = () => !document.documentElement.classList.contains('hints-off');
    function close(item) {
        item.open = false;
        item.dataset.pinned = '';
        item.querySelector('summary').setAttribute('aria-expanded', 'false');
    }
    function closeAll(except) {
        items().forEach(item => { if (item !== except) close(item); });
    }
    function position(item) {
        if (!item.isConnected) return;
        const panel = item.querySelector('.context-help__panel');
        const trigger = item.querySelector('summary').getBoundingClientRect();
        const margin = 12;
        panel.style.position = 'fixed';
        const box = panel.getBoundingClientRect();
        panel.style.left = `${Math.max(margin, Math.min(trigger.left, innerWidth - box.width - margin))}px`;
        const below = trigger.bottom + 8;
        panel.style.top = `${Math.max(margin, below + box.height <= innerHeight - margin ? below : trigger.top - box.height - 8)}px`;
    }
    function open(item) {
        if (!enabled() || !item.isConnected) return;
        closeAll(item);
        item.open = true;
        item.querySelector('summary').setAttribute('aria-expanded', 'true');
        position(item);
    }
    function initialize(item) {
        if (initialized.has(item)) return;
        const trigger = item.querySelector('summary');
        const dismiss = item.querySelector('.context-help__close');
        if (!trigger || !dismiss || !item.querySelector('.context-help__panel')) return;
        initialized.add(item);
        let hoverCloseTimer;
        trigger.setAttribute('aria-expanded', 'false');
        dismiss.hidden = false;
        dismiss.addEventListener('click', () => {
            close(item);
            suppressFocus = true;
            trigger.focus();
            suppressFocus = false;
        });
        trigger.addEventListener('click', event => {
            event.preventDefault();
            if (item.open && item.dataset.pinned === 'true') close(item);
            else { open(item); item.dataset.pinned = 'true'; }
        });
        item.addEventListener('pointerenter', event => {
            if (event.pointerType === 'mouse') { clearTimeout(hoverCloseTimer); open(item); }
        });
        item.addEventListener('pointerleave', () => {
            hoverCloseTimer = setTimeout(() => {
                if (item.dataset.pinned !== 'true' && !item.contains(document.activeElement)) close(item);
            }, 180);
        });
        item.addEventListener('focusin', event => {
            if (event.target === trigger && !suppressFocus) open(item);
        });
        item.addEventListener('focusout', event => {
            if (!item.contains(event.relatedTarget)) close(item);
        });
        item.addEventListener('toggle', () => {
            trigger.setAttribute('aria-expanded', String(item.open));
            if (item.open) position(item);
        });
    }
    function enhance(root) {
        // Also handle disclosures assembled incrementally rather than by innerHTML.
        if (root.nodeType === 1) {
            const ancestor = root.closest('.context-help');
            if (ancestor) initialize(ancestor);
        }
        root.querySelectorAll?.('.context-help').forEach(initialize);
    }
    enhance(document);
    new MutationObserver(records => {
        for (const record of records) {
            record.addedNodes.forEach(enhance);
        }
    }).observe(document.body, {childList: true, subtree: true});
    document.addEventListener('click', event => {
        if (!event.target.closest('.context-help')) closeAll();
    });
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        const active = items().find(item => item.open);
        if (!active) return;
        event.preventDefault();
        closeAll();
        suppressFocus = true;
        active.querySelector('summary').focus();
        suppressFocus = false;
    });
    addEventListener('resize', () => items().filter(item => item.open).forEach(position));
    addEventListener('scroll', () => items().filter(item => item.open).forEach(position), true);
    new MutationObserver(() => { if (!enabled()) closeAll(); }).observe(document.documentElement, {attributes: true, attributeFilter: ['class']});
    window.CompetitionsHelp = {closeAll};
})();
