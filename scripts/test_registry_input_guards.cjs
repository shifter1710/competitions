// Production Main methods in a minimal DOM. Real-browser interaction is verified independently.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let promptResult = null, confirmResult = false, confirms = 0, notifications = [];
const document = {activeElement: null, createElement: tag => new Element(tag)};
const context = {window: {addEventListener() {}}, document, FormData, URLSearchParams,
    prompt: () => promptResult, confirm: () => { confirms++; return confirmResult; },
    notify: value => notifications.push(value)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/scripts/script.js'), 'utf8') + '\nglobalThis.Controller = Main;', context);
context.notify = value => notifications.push(value);
class Element {
    constructor(tag = 'tr', key = null, value = '') {
        this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
        this.dataset = key ? {editKey: key} : {}; this.value = value; this.disabled = false;
        this.events = {}; this.classes = new Set(); this.isConnected = true; this.visible = true;
        this.classList = {add: (...names) => names.forEach(n => this.classes.add(n)),
            remove: (...names) => names.forEach(n => this.classes.delete(n)), contains: n => this.classes.has(n)};
    }
    set textContent(text) { this.text = text; this.children = []; }
    get textContent() { return this.text || ''; }
    set innerHTML(text) {
        this.backup = text; this.children = [];
        (this.originalColumns || []).forEach(key => { const cell = new Element('td'); cell.dataset.columnKey = key; this.append(cell); });
    }
    get innerHTML() {
        this.originalColumns = this.children.map(cell => cell.dataset.columnKey);
        return this.backup || 'original cells';
    }
    append(...children) { children.forEach(child => { child.parent = this; this.children.push(child); }); }
    prepend(child) { child.parent = this; this.children.unshift(child); }
    remove() { this.parent.children = this.parent.children.filter(child => child !== this); this.isConnected = false; }
    closest(selector) { return selector === 'tr' ? (this.tagName === 'TR' ? this : this.parent?.closest(selector)) : null; }
    addEventListener(name, fn) { this.events[name] = fn; }
    removeEventListener(name) { delete this.events[name]; }
    matches(selector) { return Boolean(this.dataset.editKey) && (!selector.includes(':not') || !this.disabled); }
    querySelectorAll(selector) {
        const all = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
        return all.filter(child => child.matches(selector));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    getClientRects() { return this.visible ? [{}] : []; }
    focus() { document.activeElement = this; this.closest('tr')?.events.focusin?.({target: this}); }
    scrollIntoView() { this.scrolled = true; }
}
const keys = ['student_name', 'position', 'date', 'note', 'actions'];
function existing(id) {
    const row = new Element();
    keys.forEach(key => { const cell = new Element('td'); cell.dataset.columnKey = key; row.append(cell); });
    return {row, button: {closest: () => row, dataset: {recordId: String(id), studentName: 'Синтетик Альфа',
        position: '4', date: '20-22.10.2026', extraJson: '{"note":"Исходная заметка"}'}}};
}
function main() {
    const controller = Object.create(context.Controller.prototype);
    const body = new Element('tbody');
    Object.assign(controller, {activeComboBoxes: [], activeFioResolvers: [], requests: [],
        getCustomFieldTypes: () => ({note: 'text'}), getProfileFieldDefaults: () => ({}),
        createInlineInput: (key, value) => new Element('input', key, value),
        attachRowDatePickers: () => [], linkRowCombos() {}, attachFioResolver() {},
        makeRequest(request) { this.requests.push(request); },
        refreshCurrentContent: () => Promise.resolve(),
        tableElement: {querySelector: () => body, querySelectorAll: () => keys.map(key => ({dataset: {columnKey: key}, classList: {contains: () => false}}))}});
    return controller;
}
function field(controller, key) {
    return (controller.inlineEditRow || controller.newEditRow).querySelectorAll('[data-edit-key]').find(input => input.dataset.editKey === key);
}
async function run() {
    // Strict null is cancellation; explicit empty OK remains a modifying request.
    let page = main(), trigger = new Element('button'); document.activeElement = trigger;
    for (let i = 0; i < 3; i++) page.reviewCompetition('42', 'reject');
    assert.equal(page.requests.length, 0); assert.equal(document.activeElement, trigger);
    for (const comment of ['', 'Комментарий вымышленной заявки']) {
        promptResult = comment; page.reviewCompetition('42', 'reject');
        assert.equal(page.requests.at(-1).options.body.get('comment'), comment);
    }
    assert.equal(page.requests.length, 2);

    // Switching existing/new editors, unchanged and reverted inputs, key order, focus.
    page = main(); let a = existing(1), b = existing(2); page.startInlineEdit(a.button);
    let note = field(page, 'note'), date = field(page, 'date');
    note.value = 'Сырой черновик'; date.value = '25-27.10.2026'; date.focus();
    const originalRow = page.inlineEditRow;
    document.activeElement = new Element('button'); confirms = 0; confirmResult = false;
    page.startInlineEdit(b.button);
    assert.equal(confirms, 1); assert.equal(page.inlineEditRow, originalRow);
    assert.equal(field(page, 'note'), note); assert.equal(note.value, 'Сырой черновик');
    assert.equal(date.value, '25-27.10.2026'); assert.equal(document.activeElement, date);
    page.startNewRowEdit(); assert.equal(page.inlineEditRow, originalRow); assert.equal(page.newEditRow, undefined);
    confirmResult = true; page.startNewRowEdit(); assert.equal(page.inlineEditRow, null); assert.ok(page.newEditRow);
    const newRow = page.newEditRow; field(page, 'note').value = 'Новая строка';
    page.startNewRowEdit(); assert.equal(page.newEditRow, newRow); // Reopening preserves it.
    confirmResult = false; page.startInlineEdit(b.button); assert.equal(page.newEditRow, newRow);
    confirmResult = true; page.startInlineEdit(b.button); assert.equal(page.newEditRow, null); assert.equal(page.inlineEditRow, b.row);
    confirms = 0; page.inlineEditRow.children.reverse(); // DOM order is not a change.
    field(page, 'note').value = 'Временная правка'; field(page, 'note').value = 'Исходная заметка';
    page.startInlineEdit(a.button); assert.equal(confirms, 0);
    const locked = new Element('input', 'event_owned', 'original'); locked.disabled = true; a.row.append(locked);
    locked.value = 'programmatic'; assert.equal(page.confirmRowEditSwitch(), true); assert.equal(confirms, 0);

    // Failed save preserves raw values and is protected on the next transition.
    note = field(page, 'note'); note.value = 'Черновик после отказа'; page.saveInlineEdit();
    page.saveInlineEdit(); assert.equal(page.requests.length, 1);
    page.startInlineEdit(b.button); page.startNewRowEdit(); page.cancelInlineEdit();
    assert.equal(page.inlineEditRow, a.row); assert.equal(page.requests.length, 1);
    page.requests[0].onFailure(); page.requests[0].onError('Synthetic validation failure');
    assert.equal(note.value, 'Черновик после отказа'); confirmResult = false;
    page.startInlineEdit(b.button); assert.equal(page.inlineEditRow, a.row); assert.equal(note.value, 'Черновик после отказа');
    // Lock includes the asynchronous refresh, even after successful cleanup.
    let finishRefresh; page.refreshCurrentContent = () => new Promise(resolve => { finishRefresh = resolve; });
    page.saveInlineEdit(); page.requests[1].onSuccess(); assert.equal(page.inlineEditRow, null); assert.equal(page.inlineSaving, true);
    page.startInlineEdit(b.button); page.startNewRowEdit(); assert.equal(page.inlineEditRow, null); assert.equal(page.newEditRow, null);
    finishRefresh(); await Promise.resolve(); assert.equal(page.inlineSaving, false);
    page.startInlineEdit(b.button); assert.equal(page.inlineEditRow, b.row);

    // New-row failure/pending has the same behavior; explicit cancel remains direct.
    page = main(); page.startNewRowEdit(); field(page, 'note').value = 'Новый черновик';
    page.saveNewRowEdit(); page.saveNewRowEdit(); page.cancelNewRowEdit(); page.startInlineEdit(a.button);
    assert.ok(page.newEditRow); assert.equal(page.requests.length, 1);
    page.requests[0].onFailure(); page.requests[0].onError('Synthetic failure');
    assert.equal(field(page, 'note').value, 'Новый черновик');
    confirms = 0; page.cancelNewRowEdit(); assert.equal(page.newEditRow, null); assert.equal(confirms, 0);

    // An open date/combobox/resolver consumes the first Escape before row cancellation.
    for (const widget of ['date', 'combo', 'resolver']) {
        page = main(); page.startNewRowEdit(); let cancelled = 0;
        const handler = page.bindRowKeyboardNavigation(page.newEditRow, () => {}, () => { cancelled++; });
        if (widget === 'date') page.newRowDatePickers = [{active: true}];
        if (widget === 'combo') page.activeComboBoxes = [{isOpen: () => true}];
        if (widget === 'resolver') page.activeFioResolvers = [{isOpen: () => true}];
        const event = {key: 'Escape', preventDefault() {}, stopPropagation() {}};
        handler(event); assert.equal(cancelled, 0);
        page.newRowDatePickers = []; page.activeComboBoxes = []; page.activeFioResolvers = [];
        handler(event); assert.equal(cancelled, 1);
    }
    // A single-date widget may rewrite input at construction; manual ranges stay intact.
    page = main(); page.startNewRowEdit(); const range = field(page, 'date'); range.value = '20-22.10.2026';
    context.createViewportDatepicker = input => {
        assert.equal(input.value, ''); input.value = '20.10.0011';
        return {update() { input.value = 'library normalized date'; return this; }};
    };
    const rangePicker = context.Controller.prototype.attachRowDatePickers.call(page, page.newEditRow)[0];
    assert.equal(range.value, '20-22.10.2026');
    for (let i = 0; i < 3; i++) {
        rangePicker.update({revert: true}); // Outside click/blur/return to editor.
        rangePicker.update({render: true}); // Exiting keyboard edit mode.
        assert.equal(range.value, '20-22.10.2026');
    }
    range.value = '23.10.2026'; rangePicker.update();
    assert.equal(range.value, 'library normalized date'); // Single-date widget behavior is preserved.

    // Manual import responses use native document navigation; other level forms keep their fetch path.
    const forms = [true, false].map(native => ({dataset: {}, events: {},
        hasAttribute: name => native && name === 'data-native-validation-response',
        addEventListener(name, fn) { this.events[name] = fn; }}));
    const selects = forms.map(form => ({form, parentNode: {insertBefore() {}}, nextSibling: null}));
    let enhanced = 0;
    context.enhanceLevelSelect = () => { enhanced++; return {wrapper: {}}; };
    document.querySelectorAll = () => selects;
    context.initializeLevelSelects();
    assert.equal(enhanced, 2);
    assert.equal(forms[0].events.submit, undefined);
    assert.equal(typeof forms[1].events.submit, 'function');

    // The request helper's CSRF-specific notification also releases the save lock.
    page = main(); page.inlineSaving = true; page.getCsrfToken = () => 'synthetic-csrf'; page.setLoading = () => {};
    context.fetch = () => Promise.resolve({ok: false, status: 403, text: () => Promise.resolve('CSRF token missing or invalid')});
    context.Controller.prototype.makeRequest.call(page, {url: '/competition/42', options: {method: 'POST'},
        onFailure: () => { page.inlineSaving = false; }});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(page.inlineSaving, false);
    console.log('Registry input guards PASS: null cancel, keyed draft/focus, A/B/new, pending+refresh, duplicate save, failures, popup Escape.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
