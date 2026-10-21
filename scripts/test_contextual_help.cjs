// Exercise the production helper across DOM replacement, without a DOM library.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/static/scripts/contextual-help.js'), 'utf8');

function page(initialCount = 1) {
    const observers = [];
    let document;
    class Element {
        constructor(kind) {
            this.kind = kind; this.nodeType = 1; this.events = {}; this.children = [];
            this.dataset = {}; this.style = {}; this.attributes = {}; this.hidden = true;
        }
        addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
        fire(name, fields = {}) {
            const event = {target: this, preventDefault() {}, ...fields};
            (this.events[name] || []).forEach(fn => fn(event));
        }
        append(...children) { children.forEach(child => { child.parent = this; this.children.push(child); }); }
        get isConnected() { return this === document.body || Boolean(this.parent?.isConnected); }
        closest(selector) {
            if (selector === '.context-help' && this.kind === 'help') return this;
            return this.parent?.closest(selector) || null;
        }
        contains(target) { return target === this || this.children.some(child => child.contains(target)); }
        querySelectorAll(selector) {
            return this.children.flatMap(child => [
                ...(selector === '.context-help' && child.kind === 'help' ? [child] : []),
                ...child.querySelectorAll(selector),
            ]);
        }
        querySelector(selector) {
            const kind = {'summary': 'summary', '.context-help__panel': 'panel', '.context-help__close': 'close'}[selector];
            const find = element => element.kind === kind ? element : element.children.map(find).find(Boolean);
            return find(this) || null;
        }
        setAttribute(key, value) { this.attributes[key] = value; }
        getBoundingClientRect() { return {left: 20, top: 20, bottom: 40, width: 300, height: 150}; }
        focus() {
            document.activeElement = this;
            this.closest('.context-help')?.fire('focusin', {target: this});
        }
    }
    document = new Element('document'); document.nodeType = 9;
    document.body = new Element('body'); document.append(document.body);
    const classes = new Set();
    document.documentElement = {classList: {contains: value => classes.has(value)}};
    class Observer {
        constructor(callback) { this.callback = callback; observers.push(this); }
        observe(target, options) { this.target = target; this.options = options; }
    }
    function help() {
        const item = new Element('help'); item.open = false;
        const summary = new Element('summary'), panel = new Element('panel'), close = new Element('close');
        panel.append(close); item.append(summary, panel);
        return item;
    }
    for (let index = 0; index < initialCount; index++) document.body.append(help());
    const context = {document, MutationObserver: Observer, innerWidth: 1280, innerHeight: 800,
        setTimeout, clearTimeout, addEventListener() {}, window: {}};
    vm.runInNewContext(source, context);
    function notify(root) {
        observers.filter(observer => observer.target === document.body)
            .forEach(observer => observer.callback([{addedNodes: [root]}]));
    }
    function replace() {
        document.body.children.forEach(child => { child.parent = null; });
        const wrapper = new Element('wrapper'), item = help();
        wrapper.append(item); document.body.children = []; document.body.append(wrapper); notify(wrapper);
        return item;
    }
    function mode(off) {
        off ? classes.add('hints-off') : classes.delete('hints-off');
        observers.filter(observer => observer.target === document.documentElement).forEach(observer => observer.callback([]));
    }
    return {document, help, replace, notify, mode, api: context.window.CompetitionsHelp};
}

for (const initialCount of [0, 1]) {
    const p = page(initialCount);
    const original = p.document.querySelectorAll('.context-help')[0];
    if (original) original.open = true;
    const item = p.replace();
    const trigger = item.querySelector('summary'), dismiss = item.querySelector('.context-help__close');
    assert.equal(dismiss.hidden, false, 'replacement gets mobile dismiss button');
    item.fire('pointerenter', {pointerType: 'mouse'});
    assert.equal(item.open, true, 'hover opens replacement');
    p.document.fire('keydown', {key: 'Escape'});
    assert.equal(item.open, false, 'Escape finds the connected replacement');
    assert.equal(trigger.attributes['aria-expanded'], 'false');
    trigger.focus(); assert.equal(item.open, true, 'keyboard focus opens replacement');
    dismiss.fire('click'); assert.equal(item.open, false, 'dismiss does not reopen on focus');
    assert.equal(p.document.activeElement, trigger);
    // Observer can see both a wrapper and its descendants: binding stays idempotent.
    p.notify(item); p.notify(trigger);
    assert.equal(trigger.events.click.length, 1, 'only one click handler after repeated observation');
    trigger.fire('click'); assert.equal(item.open, true, 'touch/click opens');
    trigger.fire('click'); assert.equal(item.open, false, 'second click closes');
    trigger.fire('click'); p.mode(true); assert.equal(item.open, false, 'bulb off closes replacement');
    item.fire('pointerenter', {pointerType: 'mouse'}); assert.equal(item.open, false, 'off prevents reopening');
    p.mode(false); assert.equal(item.open, false, 'bulb on leaves help closed');
    trigger.fire('click'); p.api.closeAll(); assert.equal(item.open, false, 'public closeAll uses live DOM');
    if (original) assert.equal(original.open, true, 'global actions ignore detached nodes');
    const second = p.replace();
    second.querySelector('summary').fire('click');
    p.document.fire('click', {target: p.document.body});
    assert.equal(second.open, false, 'outside click closes after repeated replacement');
}
console.log('Contextual help PASS: dynamic DOM, live global operations, idempotence, keyboard, mobile and global mode.');
