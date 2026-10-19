// Run the actual Calendar controller in a small event-capable form, with no DOM library.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Field {
    constructor(value = '') { this.value = value; this.events = {}; }
    addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
    fire(name) { (this.events[name] || []).forEach(fn => fn({target: this})); }
}
function calendar({start = '', end = '', today = '19.10.2026', optIn = true, mode = 'custom'} = {}) {
    const select = new Field(mode), from = new Field(start), to = new Field(end);
    const dates = {querySelectorAll: () => [from, to]};
    const form = new Field(); form.dataset = optIn ? {periodToday: today} : {};
    form.querySelector = selector => ({'[data-period-choice]': select, '[data-period-dates]': dates,
        '[name="date_from"]': from, '[name="date_to"]': to})[selector] || null;
    const document = {querySelectorAll: selector => selector === 'form[data-view-period-form]' ? [form] : [],
        querySelector: () => null, addEventListener: (name, fn) => fn()};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/static/scripts/period-filters.js'), 'utf8'), {document, setTimeout: fn => fn()});
    return {select, from, to, form, dates};
}
let page = calendar();
assert.equal(page.to.value, ''); // No insertion on load or mode switch.
page.select.fire('change'); assert.equal(page.to.value, '');
page.from.value = '01.09.2026'; page.from.fire('input');
assert.equal(page.to.value, '19.10.2026');
page.from.value = '20.10.2026'; page.from.fire('change');
assert.equal(page.to.value, '19.10.2026'); // Server handles a subsequently reversed interval.
page = calendar(); page.from.value = '20.10.2026'; page.from.fire('input'); assert.equal(page.to.value, '');
page.from.value = '31.02.2026'; page.from.fire('change'); assert.equal(page.to.value, '');
page.from.value = '29.02.2024'; page.from.fire('change'); assert.equal(page.to.value, '19.10.2026');
for (const manual of ['', '01.10.2026']) {
    page = calendar(); page.to.value = manual; page.to.fire('input');
    page.from.value = '01.09.2026'; page.from.fire('change'); assert.equal(page.to.value, manual);
    page.select.value = 'all'; page.select.fire('change'); assert.equal(page.to.disabled, true);
    page.select.value = 'custom'; page.select.fire('change'); page.from.fire('input'); assert.equal(page.to.value, manual);
}
page = calendar({start: '01.09.2026'}); assert.equal(page.to.value, '');
page = calendar({end: 'malformed-url-value'}); page.from.value = '01.09.2026'; page.from.fire('input'); assert.equal(page.to.value, 'malformed-url-value');
page = calendar({optIn: false}); page.from.value = '01.09.2026'; page.from.fire('input'); assert.equal(page.to.value, '');
page = calendar({today: '01.01.2027'}); page.from.value = '31.12.2026'; page.from.fire('input'); assert.equal(page.to.value, '01.01.2027');
console.log('Calendar period autofill PASS: explicit server date, user intent, restoration, leap/future dates, opt-in and hidden fields.');
