// Exercise production draft-period filtering/navigation, including review and applied state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {window: {addEventListener() {}}, console, URLSearchParams};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/scripts/script.js'), 'utf8') + '\nglobalThis.MainController = Main;', context);
const Main = context.MainController;
const text = value => ({textContent: value, hidden: false, setAttribute() {}});
const controls = {'.report__period': {value: 'custom'}, '[name="date_from"]': {value: '01.09.2026'}, '[name="date_to"]': {value: '31.10.2026'}};
const events = [
    ['1','2026-03-01','2026-03-01'], ['2','2026-09-01','2026-09-01'],
    ['3','2026-08-31','2026-09-01'], ['4','2026-10-31','2026-11-02'],
    ['5','2026-11-01','2026-11-01'], ['6','2026-08-01','2026-11-30'],
    ['7','2026-10-01','2026-10-01'], ['999','',''],
];
const list = {appendChild() { throw new Error('Named controls must keep DOM positions'); }};
// Reparenting named controls breaks native positional Back restoration.
const reviewList = {appendChild() { throw new Error('Named controls must never move for visual review'); }};
const options = events.map(([id, from, to]) => {
    const label = text(id === '2' || id === '7' ? 'Одинаковое название' : `Демо ${id}`);
    const outside = text('Вне периода'); const detail = text(from);
    const option = {dataset: {dateFrom: from, dateTo: to}, parentElement: list, innerText: label.textContent};
    const input = {value: id, checked: false, closest: () => option}; option.input = input;
    option.querySelector = selector => selector === 'input' ? input : selector === '.report-multi__outside' ? outside : label;
    option.querySelectorAll = () => [detail]; return option;
});
const search = {value: ''}; const empty = text(''); const review = text(''); const status = text(''); const toggle = text('');
const nodes = {'.report-multi__search': search, '.report-multi__empty': empty, '.report-multi__review': review,
    '.report-multi__review-list': reviewList, '.report-multi__list': list, '#report-event-review-summary': status,
    '.report-multi__toggle': toggle, '.report-multi__value': text(''), '.report-multi__count': text('')};
const control = {dataset: {filterKey: 'event_id', label: 'Соревнования'}, querySelector: selector => nodes[selector],
    querySelectorAll: selector => selector === '.report-multi__option' ? options : options.filter(o => o.input.checked).map(o => o.input)};
let submitted = 0, focused = 0;
const form = {dataset: {appliedQuery: 'period=custom&date_from=01.09.2026&date_to=31.10.2026&event_id=1'},
    querySelector: selector => controls[selector] || (selector === '.report-multi[data-filter-key="group"]' ? null : selector === '#report-event-review-summary' ? status : {focus() { focused++; }}),
    querySelectorAll: selector => selector === '.report-multi' ? [control] : [], submit() { submitted++; }};
const controller = Object.create(Main.prototype); controller.reportForm = form;
controller.updateReportDirtyState = () => {};
controller.serializeReportForm = () => [['period', controls['.report__period'].value], ['date_from', controls['[name="date_from"]'].value], ['date_to', controls['[name="date_to"]'].value], ...options.filter(o => o.input.checked).map(o => ['event_id', o.input.value])];
controller.reportFormIsDirty = () => false; // Clean restored URL: no automatic loss of applied constraint.
function available() { controller.refreshReportMultiselects(); return options.filter(o => o.dataset.available === 'true').map(o => o.input.value); }
assert.deepEqual(available(), ['2','3','4','6','7']);
options[0].input.checked = true; options[7].input.checked = true;
search.value = 'no matches'; available();
assert.equal(options[0].hidden, false); assert.equal(options[0].parentElement, list); assert.equal(options[0].dataset.review, "true");
assert.equal(options[7].hidden, false); assert.equal(status.hidden, false); assert.equal(review.hidden, false);
assert.equal(empty.hidden, false); assert.equal(controller.reportEventReviewCount, 2);
// Native Tab follows visual available -> review order in both directions;
// neither hidden options nor the form's surrounding controls are skipped.
search.value = ''; available();
let tabTarget = '';
const focusable = id => ({getClientRects: () => [1], focus() { tabTarget = id; }});
const before = focusable('before'), after = focusable('after');
options.forEach(option => Object.assign(option.input, {name: 'event_id', type: 'checkbox',
    getClientRects: () => option.hidden ? [] : [1], focus() { tabTarget = this.value; }}));
const queryAll = form.querySelectorAll;
form.querySelectorAll = selector => selector === 'input, select, button, a[href]' ? [before, ...options.map(o => o.input), after] : queryAll(selector);
for (const [target, shift, expected] of [[before, false, '2'], [options[6].input, false, '1'],
    [options[7].input, false, 'after'], [after, true, '999'], [options[1].input, true, 'before']]) {
    let prevented = false;
    controller.handleReportOptionsTab({key: 'Tab', target, shiftKey: shift, preventDefault() { prevented = true; }});
    assert.equal(prevented, true); assert.equal(tabTarget, expected);
}
controller.handleReportOptionsTab({key: ' ', preventDefault() { throw new Error('Space must stay native'); }});
controller.reportInitialEntriesKey = Main.reportEntriesKey(Main.canonicalReportEntries(controller.serializeReportForm()));
controller.navigateReportForm(); assert.equal(submitted, 1); assert.equal(focused, 0);
controls['[name="date_to"]'].value = '30.10.2026'; controller.navigateReportForm(); assert.equal(submitted, 1); assert.equal(focused, 1);
// Every available ID (including same names) remains distinct; unavailable IDs cannot be selected by All.
options.forEach(o => { o.input.checked = false; }); available();
options.forEach(o => { if (o.dataset.available !== 'false') o.input.checked = true; });
assert.deepEqual(options.filter(o => o.input.checked).map(o => o.input.value), ['2','3','6','7']);
controls['.report__period'].value = '2026-2027'; search.value = '';
assert.deepEqual(available(), ['2','3','4','5','6','7']);
controls['.report__period'].value = 'all'; assert.deepEqual(available(), ['1','2','3','4','5','6','7']);
controls['.report__period'].value = 'custom'; controls['[name="date_from"]'].value = '31.02.2026';
assert.equal(controller.reportDraftPeriod().valid, false); available(); assert.match(status.textContent, /корректные границы/);
assert.ok(options.every(option => option.parentElement === list));
assert.equal(Main.reportDateISO('29.02.2024'), '2024-02-29'); assert.equal(Main.reportDateISO('29.02.2026'), null);
// A valid legacy single-bound URL remains unbounded on its other side.
form.dataset.appliedQuery = 'date_from=01.09.2026'; controls['[name="date_from"]'].value = '01.09.2026'; controls['[name="date_to"]'].value = '';
assert.equal(controller.reportDraftPeriod().valid, true); assert.deepEqual(available(), ['2','3','4','5','6','7']);
controls['[name="date_from"]'].value = '02.09.2026'; assert.equal(controller.reportDraftPeriod().valid, false);
console.log('Reports period options PASS: inclusive overlap, namesake IDs, academic/all/legacy, review/search, dirty navigation and invalid dates.');
