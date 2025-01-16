# SIBADI Competitions
Сервис для учета участия в спортивных соревнованиях студентов [СибАДИ](https://sibadi.org/).

## Technical
- Backend built with [Sanic](https://sanic.dev/en/)
- Templates built with [Jinja2](https://jinja.palletsprojects.com/en/3.1.x/) 
- Frontend built with vanila JavaScript

## Development

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
