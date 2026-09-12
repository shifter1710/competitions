# Стадия 1: минификация клиентского JS (terser).
# В репозитории script.js хранится читаемым; в образ попадает
# минифицированная копия по тому же пути (шаблоны и кэш не меняются).
FROM node:22-slim AS jsbuild

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

COPY scripts/terser-mangle-props.mjs ./
COPY src/static/scripts/script.js ./
# --mangle переименовывает локальные переменные, но не методы/поля класса
# (this.makeRequest и т.п.); blanket --mangle-props ломает чтение полей JSON
# ответов сервера. Поэтому манглим свойства только по списку имён, доказанно
# внутренних для классов script.js (извлекает terser-mangle-props.mjs).
RUN PROPS="$(node terser-mangle-props.mjs script.js)" \
    && npx terser script.js --compress --mangle --toplevel --ecma 2022 \
        --mangle-props "regex=$PROPS" -o script.min.js \
    && mv script.min.js script.js

# Стадия 2: образ приложения.
FROM python:3.10-slim

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY ./src /code/src
COPY --from=jsbuild /build/script.js /code/src/static/scripts/script.js
RUN mkdir /code/data

CMD ["sh", "-c", "sanic src.main:app --host ${WEB_HOST:-0.0.0.0} --port ${WEB_PORT:-8080}"]
