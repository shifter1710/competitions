// Exercise the production controller methods with a minimal table DOM.
// Independent QA additionally verifies browser layout and interaction.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {window: {addEventListener() {}}, console};
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
console.log('Reports table regression PASS: all columns, both directions, ties, rowspan, renumbering, column order/hidden and generic table path.');
