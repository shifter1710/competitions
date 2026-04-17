# SIBADI Competitions
Сервис для учета участия в спортивных соревнованиях студентов [СибАДИ](https://sibadi.org/).

## Technical
- Backend built with [Sanic](https://sanic.dev/en/)
- Templates built with [Jinja2](https://jinja.palletsprojects.com/en/3.1.x/) 
- Frontend built with vanila JavaScript

## Development

### Local run

1. Create environment file
```
cp .env.example .env
```

2. Install requirements
```
pip install -r requirements.txt
```

3. Start application
```
sanic src.main:app --host 0.0.0.0 --port 8080
```

By default the app uses SQLite at `./data/competitions.sqlite3`.

The app also requires authentication settings. For local development, set at least:
```
AUTH_SECRET_KEY=replace-with-random-string
AUTH_ADMIN_USERNAME=admin
AUTH_ADMIN_PASSWORD=strong-password
```

Optional read-only user:
```
AUTH_VIEWER_USERNAME=viewer
AUTH_VIEWER_PASSWORD=viewer-password
```

### Docker run

```
docker compose up -d --build
```

The container is published on `127.0.0.1:8081`, intended to be proxied by nginx.
Application data, including the SQLite database, is stored in `./data`.

### Migrating existing MongoDB data

For one-time migration from MongoDB to SQLite, use `scripts/migrate_mongo_to_sqlite.py`.
The migration helper is not part of the runtime container and expects `pymongo` to be available in the environment where you run the script.

### Linting

Python linters in this project are set up as [pre-commit](https://pre-commit.com/) hook. Do these steps to use them:
1. Install requirements
```
pip install -r requirements.txt
```

2. Initialize pre-commit hook
```
pre-commit install
```

3. Run linters
```
pre-commit run --all-files
```

When linters are set, they now will be trigered any time you do commit. If there are any errors detected before commit, fix them, then do `git add .` and commit once again.

## Договоренности
### Порядок атрибутов
На главной и в excel-файле: ФИО	Пол	Институт Группа Вид спорта Дата Уровень соревнований Название соревнований Место Курс

### Время
Время создания записи всегда устанавливается по UTC

## Deployment

Deployment templates are prepared for `dokin-app.online`:

- docker compose: [docker-compose.yml](/root/chatgpt/competitions/docker-compose.yml)
- systemd unit: [deploy/systemd/competitions-compose.service](/root/chatgpt/competitions/deploy/systemd/competitions-compose.service)
- nginx config: [deploy/nginx/competitions.conf](/root/chatgpt/competitions/deploy/nginx/competitions.conf)
