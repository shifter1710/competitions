// Exercise the production controller methods with a minimal table DOM.
// Independent QA additionally verifies browser layout and interaction.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {window: {addEventListener() {}}, console, URLSearchParams};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/scripts/script.js'), 'utf8') + '\nglobalThis.MainController = Main;', context);
class Element {
    constructor(key, text = '', children = []) {
        this.dataset = key ? {columnKey: key} : {};
        this.textContent = String(text);
        this.children = [];
        this.attrs = {};
        this.events = {};
        this.classes = new Set();
        this.classList = {
            add: (...names) => names.forEach(name => this.classes.add(name)),
            remove: (...names) => names.forEach(name => this.classes.delete(name)),
            toggle: (name, value) => value ? this.classes.add(name) : this.classes.delete(name),
            contains: name => this.classes.has(name),
        };
        children.forEach(child => this.appendChild(child));
    }
    appendChild(child) {
        if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
        this.children.push(child); child.parent = this;
    }
    insertBefore(child, before) {
        if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
        this.children.splice(this.children.indexOf(before), 0, child); child.parent = this;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) {
        if (selector === 'thead th') return this.head.children;
        if (selector === 'tbody') return [this.body];
        if (selector === 'tr') return this.head ? [this.head, ...this.body.children, this.foot] : this.children;
        if (selector === '[data-column-key]') return this.querySelectorAll('tr').flatMap(row => row.children);
        const key = /data-column-key="([^"]+)"/.exec(selector)?.[1];
        if (selector.startsWith('thead th[')) return this.head.children.filter(cell => cell.dataset.columnKey === key);
        return this.children.filter(cell => cell.dataset.columnKey === key);
    }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
    addEventListener(name, fn) { this.events[name] = fn; }
}
const keys = ['index', 'student_name', 'student_sex', 'institute', 'group', 'course', 'count_participation', 'count_wins', 'count_prizes'];
function createTable(report = true) {
    const rows = [
        ['a1', 'a', 'Альфа', 'М', 'А', 'А-1', 1, 1, 0, 1],
        ['b1', 'b', 'Бета', 'Ж', 'Б', 'Б-2', 2, 3, 1, 2],
        ['a2', 'a', null, 'М', 'В', 'А-3', 3, 5, 2, 3],
        ['c1', 'c', 'Альфа', 'М', 'А', 'А-1', 1, 0, 0, 0],
        ['d1', 'd', 'Альфа', 'М', 'А', 'А-1', 1, 0, 0, 0],
    ].map(([id, group, name, ...values], index) => {
        const row = new Element(); row.id = id; row.dataset.reportGroup = group;
        keys.forEach((key, i) => {
            const value = [index + 1, name, ...values][i];
            if (value !== null) row.appendChild(new Element(key, value));
        });
        return row;
    });
    const table = new Element(); table.dataset.reportTable = report ? 'true' : '';
    table.head = new Element(null, '', keys.map(key => {
        const header = new Element(key);
        header.dataset.sortType = key === 'index' ? 'none' : key === 'course' || key.startsWith('count_') ? 'number' : 'text';
        return header;
    }));
    table.body = new Element(null, '', rows);
    table.foot = new Element(null, '', keys.map(key => new Element(key, 'total')));
    const controller = Object.create(context.MainController.prototype);
    controller.tableElement = table; controller.hasActiveRowEdit = () => false;
    return {table, controller, rows};
}
function invariants({table, rows}) {
    assert.equal(table.body.children.length, rows.length);
    assert.equal(new Set(table.body.children).size, rows.length);
    assert.deepEqual(table.body.children.map(row => row.querySelector('[data-column-key="index"]').textContent), ['1','2','3','4','5']);
    const groupRows = table.body.children.filter(row => row.dataset.reportGroup === 'a');
    assert.equal(table.body.children.indexOf(groupRows[1]), table.body.children.indexOf(groupRows[0]) + 1);
    assert.equal(groupRows[0].querySelector('[data-column-key="student_name"]').rowSpan, 2);
    assert.equal(groupRows[1].querySelector('[data-column-key="student_name"]'), null);
    assert.equal(table.body.children.flatMap(row => row.children).filter(cell => cell.dataset.columnKey === 'student_name').length, 4);
    assert.equal(table.foot.children[0].textContent, 'total');
    const order = table.head.children.map(cell => cell.dataset.columnKey);
    table.body.children.forEach(row => assert.deepEqual(row.children.map(cell => cell.dataset.columnKey), order.filter(key => row.querySelector(`[data-column-key="${key}"]`))));
}
const fixture = createTable();
fixture.controller.bindTableHeaderEvents();
assert.equal(fixture.controller.getHeaderByKey('index').events.keydown, undefined);
const quantity = fixture.controller.getHeaderByKey('count_participation');
quantity.events.keydown({key: 'Enter', preventDefault() {}});
invariants(fixture);
assert.deepEqual(fixture.table.body.children.map(row => row.id), ['c1','d1','a1','a2','b1']);
quantity.events.keydown({key: ' ', preventDefault() {}});
invariants(fixture);
assert.deepEqual(fixture.table.body.children.map(row => row.id), ['a2','a1','b1','c1','d1']);
assert.equal(quantity.attrs['aria-sort'], 'descending');
// A moved shared cell keeps hidden state; moving columns must use keys on child rows.
fixture.controller.setHiddenColumns(['student_name', 'course']);
fixture.controller.moveColumn('student_name', 7);
fixture.controller.moveColumn('course', 1);
for (const key of keys.slice(1)) {
    const header = fixture.controller.getHeaderByKey(key);
    fixture.controller.sortByHeader(header); invariants(fixture);
    fixture.controller.sortByHeader(header); invariants(fixture);
}
assert.ok(fixture.table.body.children.flatMap(row => row.children).filter(cell => ['student_name','course'].includes(cell.dataset.columnKey)).every(cell => cell.classes.has('table-column-hidden')));
// Apply a saved order and hidden preferences through the real restore path.
fixture.controller.readTableState = () => ({order: [...keys].reverse(), hidden: ['student_name']});
fixture.controller.renderColumnManager = () => {};
fixture.controller.applyTableState();
invariants(fixture);
assert.deepEqual(fixture.table.head.children.map(cell => cell.dataset.columnKey), [...keys].reverse());
assert.ok(fixture.table.body.children.flatMap(row => row.children).filter(cell => cell.dataset.columnKey === 'student_name').every(cell => cell.classes.has('table-column-hidden')));
fixture.controller.setHiddenColumns([]);
fixture.controller.reorderReportCells(keys);
invariants(fixture);
const generic = createTable(false);
// Complete physical cells: the old path used by registry/event tables stays intact.
generic.rows[2].insertBefore(new Element('student_name', 'Альфа'), generic.rows[2].children[1]);
generic.controller.moveColumn('course', 1);
assert.ok(generic.table.querySelectorAll('tr').every(row => row.children[1].dataset.columnKey === 'course'));
const sliced = createTable();
sliced.rows.forEach((row, index) => { row.dataset.reportGroup = String(index); });
sliced.controller.sortByHeader(sliced.controller.getHeaderByKey('count_participation'));
assert.deepEqual(sliced.table.body.children.map(row => row.id), ['c1','d1','a1','b1','a2']);
assert.deepEqual(sliced.table.body.children.map(row => row.querySelector('[data-column-key="index"]').textContent), ['1','2','3','4','5']);
// Server-sorted Reports keep shared drag handlers while native anchors own sorting.
const serverReport = createTable();
serverReport.table.dataset.serverSort = 'true';
let persisted = {order: [...keys], hidden: []};
serverReport.controller.readTableState = () => persisted;
serverReport.controller.saveTableState = state => { persisted = state; };
serverReport.controller.renderColumnManager = () => {};
serverReport.controller.bindTableHeaderEvents();
const beforeIDs = serverReport.table.body.children.map(row => row.id);
const groupHeader = serverReport.controller.getHeaderByKey('group');
const instituteHeader = serverReport.controller.getHeaderByKey('institute');
assert.ok(groupHeader.classes.has('table-header-cell'));
for (const header of serverReport.table.head.children) {
    assert.equal(header.events.click, undefined, 'Report sorting must remain native GET navigation');
    assert.equal(header.events.keydown, undefined, 'Report sorting must not reorder rows on the client');
    for (const name of ['dragstart', 'dragover', 'drop']) assert.equal(typeof header.events[name], 'function');
}
groupHeader.events.dragstart({dataTransfer: {}});
instituteHeader.events.dragover({preventDefault() {}});
instituteHeader.events.drop({preventDefault() {}});
assert.deepEqual([...persisted.order], ['index','student_name','student_sex','group','institute','course','count_participation','count_wins','count_prizes']);
assert.deepEqual(serverReport.table.head.children.map(header => header.dataset.columnKey), [...persisted.order]);
assert.deepEqual(serverReport.table.body.children.map(row => row.id), beforeIDs);
serverReport.table.body.children.forEach(row => assert.deepEqual(row.children.map(cell => cell.dataset.columnKey), [...persisted.order].filter(key => row.querySelector(`[data-column-key="${key}"]`))));
// ORG-01 registry server sorting: keyboard and mouse share the guarded GET action.
const registry = createTable(false);
registry.table.dataset.serverSort = 'true';
const organizerHeader = new Element('organizer');
Object.assign(organizerHeader.dataset, {sortType: 'text', sortable: 'true', sortDirection: 'asc'});
organizerHeader.setAttribute('aria-sort', 'ascending'); // Rendered by the server; keep this state.
registry.table.head.appendChild(organizerHeader);
registry.controller.bindTableHeaderEvents();
assert.equal(organizerHeader.tabIndex, 0, 'Sortable registry headers must enter the Tab order');
assert.equal(organizerHeader.attrs['aria-sort'], 'ascending');
assert.equal(registry.controller.getHeaderByKey('index').tabIndex, undefined);
assert.equal(registry.controller.getHeaderByKey('index').events.keydown, undefined);
const registryQuery = '?name=%D0%94%D0%B5%D0%BC%D0%BE&sport=One&sport=Two&per_page=10&page=4&sort=organizer&order=asc';
function setRegistryURL(search = registryQuery) { context.window.location = {search, href: 'unchanged'}; }
function verifyRegistryNavigation(order, columnKey = 'organizer') {
    const params = new URL(context.window.location.href, 'http://example.invalid').searchParams;
    assert.equal(params.get('sort'), columnKey); assert.equal(params.get('order'), order);
    assert.equal(params.get('name'), 'Демо'); assert.deepEqual(params.getAll('sport'), ['One', 'Two']);
    assert.equal(params.get('per_page'), '10'); assert.equal(params.has('page'), false);
}
for (const key of ['Enter', ' ']) {
    setRegistryURL(); let prevented = false;
    organizerHeader.events.keydown({key, preventDefault() { prevented = true; }});
    assert.equal(prevented, true, `${key} must prevent its default browser action`);
    verifyRegistryNavigation('desc');
}
organizerHeader.dataset.sortDirection = 'desc'; setRegistryURL();
organizerHeader.events.keydown({key: 'Enter', preventDefault() {}});
verifyRegistryNavigation('asc');
setRegistryURL(); organizerHeader.events.click(); verifyRegistryNavigation('asc');
registry.controller.hasActiveRowEdit = () => true;
for (const key of ['Enter', ' ']) {
    setRegistryURL(); let prevented = false;
    organizerHeader.events.keydown({key, preventDefault() { prevented = true; }});
    assert.equal(prevented, true); assert.equal(context.window.location.href, 'unchanged');
}
setRegistryURL(); organizerHeader.events.click(); assert.equal(context.window.location.href, 'unchanged');
registry.controller.hasActiveRowEdit = () => false;
organizerHeader.events.keydown({key: 'Escape', preventDefault() { throw new Error('Unrelated keys must stay native'); }});
assert.equal(context.window.location.href, 'unchanged');
const keyboardHandler = organizerHeader.events.keydown;
registry.controller.bindTableHeaderEvents();
assert.equal(organizerHeader.events.keydown, keyboardHandler, 'Repeated binding must preserve one handler');
// Both legal stored custom keys keep distinct, keyboard-sortable DOM aliases.
for (const columnKey of ['custom:organizer', 'custom__organizer']) {
    const customHeader = new Element(columnKey);
    Object.assign(customHeader.dataset, {sortType: 'text', sortable: 'true'});
    registry.table.head.appendChild(customHeader);
    registry.controller.bindTableHeaderEvents();
    assert.equal(registry.controller.getHeaderByKey(columnKey), customHeader);
    assert.equal(customHeader.tabIndex, 0);
    setRegistryURL(); customHeader.events.keydown({key: 'Enter', preventDefault() {}});
    verifyRegistryNavigation('asc', columnKey);
    customHeader.dataset.sortDirection = 'asc';
    setRegistryURL(); customHeader.events.keydown({key: ' ', preventDefault() {}});
    verifyRegistryNavigation('desc', columnKey);
}

console.log('Reports table regression PASS: all columns, both directions, ties, rowspan, renumbering, column order/hidden and generic table path.');
