#!/usr/bin/env node
// Извлекает из клиентского script.js имена, безопасные для манглинга СВОЙСТВ,
// и печатает регулярное выражение для `terser --mangle-props regex=...`.
//
// Зачем: обычный `--mangle` переименовывает локальные переменные, но НЕ методы
// и поля класса (`this.makeRequest` и т.п.). Blanket `--mangle-props` ломает
// код: он переименует и поля JSON-ответов сервера (d.records, d.success),
// которые читаются только с чужих приёмников.
//
// Принцип безопасности: манглим ТОЛЬКО имена, доказанно внутренние для
// классов файла — методы классов и свойства, используемые исключительно через
// `this.X`. Любое имя, встречающееся как свойство НЕ-this приёмника (DOM,
// распарсенный JSON, параметры) или как ключ объектного литерала,
// исключается. Ошибка извлечения в худшем случае оставит имя читаемым,
// но не сломает код.
//
// Использование: node scripts/terser-mangle-props.mjs src/static/scripts/script.js
// Печатает /^(?:name1|name2|...)$/ в stdout.

import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
// Публичный экспорт terser не содержит AST-классов, берём их из lib/ пакета
// (версия зафиксирована package-lock.json, layout стабилен в пределах terser 5).
const terserRoot = path.dirname(path.dirname(require.resolve("terser")));
const {
    AST_Class,
    AST_Dot,
    AST_Object,
    AST_String,
    AST_Sub,
    AST_This,
    TreeWalker,
} = require(path.join(terserRoot, "lib/ast.js"));
const { parse } = require(path.join(terserRoot, "lib/parse.js"));

// Встроенные имена, которые манглить нельзя никогда (страховка поверх
// собственного exclusion-списка terser).
const STOP = new Set([
    "constructor",
    "prototype",
    "__proto__",
    "name",
    "length",
    "call",
    "apply",
    "bind",
]);

const input = process.argv[2];
if (!input) {
    console.error("usage: node terser-mangle-props.mjs <script.js>");
    process.exit(1);
}

const ast = parse(readFileSync(input, "utf8"));

// Кандидаты: методы классов и this.X. Чужие: свойства не-this приёмников,
// строки-ключи obj["..."], ключи объектных литералов.
const candidates = new Set();
const foreign = new Set();

ast.walk(
    new TreeWalker((node) => {
        if (node instanceof AST_Class) {
            for (const member of node.properties) {
                if (member.key && typeof member.key.name === "string") {
                    candidates.add(member.key.name);
                }
            }
        }
        if (node instanceof AST_Dot) {
            if (node.expression instanceof AST_This) {
                candidates.add(node.property);
            } else {
                foreign.add(node.property);
            }
        }
        if (node instanceof AST_Sub && node.property instanceof AST_String) {
            foreign.add(node.property.value);
        }
        if (node instanceof AST_Object) {
            for (const prop of node.properties) {
                if (typeof prop.key === "string") {
                    foreign.add(prop.key);
                }
            }
        }
        return false;
    }),
);

const mangleable = [...candidates]
    .filter((name) => !foreign.has(name) && !STOP.has(name))
    .sort();

process.stdout.write(`/^(?:${mangleable.join("|")})$/`);
