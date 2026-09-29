"""Маршруты аутентификации и профиля: вход/выход, кабинет, /api/students/lookup.

Перенесено из src/main.py без изменения поведения (Architecture v1)."""
import hashlib
import logging
import time

from sanic import redirect
from sanic import Request
from sanic import Sanic
from sanic import text
from sanic.response import json as json_response
from sanic_ext import render

from src.auth import ATHLETE_ROLE
from src.auth import authenticate_user
from src.auth import clear_auth_cookie
from src.auth import log_audit_event
from src.auth import MODERATOR_ROLES
from src.auth import set_auth_cookie
from src.auth import user_is_athlete
from src.web import forbidden
from src.web import get_auth_user
from src.web import get_current_user_id
from src.web import get_form_value
from src.web import get_storage
from src.web import jinja_env
from src.web import unauthorized

logger = logging.getLogger(__name__)


LOGIN_MAX_ATTEMPTS = 5


LOGIN_WINDOW_SECONDS = 180


LOGIN_LOCKOUT_SECONDS = 180


LOGIN_REJECT_STATUS = 429


login_failures: dict[str, list] = {}


def register_login_failure(ip: str):
    now = time.monotonic()
    attempts = [stamp for stamp in login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    login_failures[ip] = attempts
    return len(attempts)


def login_is_locked(ip: str) -> bool:
    now = time.monotonic()
    attempts = [stamp for stamp in login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    login_failures[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def clear_login_failures(ip: str):
    login_failures.pop(ip, None)


PROFILE_FIELDS = ('student_name', 'student_sex', 'institute', 'group', 'course')


def read_profile_payload(request: Request) -> tuple[dict, str | None]:
    profile = {}
    for field in PROFILE_FIELDS:
        value = get_form_value(request, field).strip()
        if value:
            profile[field] = value
    if profile.get('student_sex') not in (None, '', 'М', 'Ж'):
        return {}, 'Пол должен быть М или Ж'
    if 'course' in profile:
        try:
            profile['course'] = str(int(profile['course']))
        except ValueError:
            return {}, 'Курс должен быть числом'
    return profile, None


# C901 (осознанное подавление): mccabe суммирует сложность вложенных
# verbatim-хендлеров, перенесённых из main.py без изменений; разбиение
# register() — Architecture v2, не pre-merge gate.
def register(app: Sanic) -> None:  # noqa: C901
    @app.get('/login')
    async def login_page(request: Request):
        if get_auth_user(request):
            return redirect('/')

        return await render(
            template_name=jinja_env.get_template('login.html'),
            context={
                'request': request,
                'error_message': request.args.get('error'),
            },
        )

    @app.post('/login')
    async def login(request: Request):
        # client_ip учитывает PROXIES_COUNT=1: за nginx это реальный IP клиента
        # (последняя запись X-Forwarded-For), а не адрес прокси.
        if login_is_locked(request.client_ip):
            return text(body='Слишком много неудачных попыток входа. Повторите позже', status=LOGIN_REJECT_STATUS)

        username = str(get_form_value(request, 'username')).strip()
        password = str(get_form_value(request, 'password'))

        user = authenticate_user(request, username, password)
        if user is None:
            register_login_failure(request.client_ip)
            log_audit_event(request, 'login_failed', username=username)
            response = await render(
                template_name=jinja_env.get_template('login.html'),
                context={
                    'request': request,
                    'error_message': 'Неверный логин или пароль',
                },
            )
            response.status = 401
            return response

        clear_login_failures(request.client_ip)
        log_audit_event(
            request,
            'login_success',
            user_id=user.get('id'),
            username=user['username'],
        )
        try:
            # Присутствие (№17): успешный вход фиксирует last_login_at
            # (и last_seen — вход тоже активность).
            get_storage(request.app).set_user_last_login(user['id'])
        except Exception:
            logger.exception('Failed to update last_login_at')
        response = redirect('/')
        set_auth_cookie(request, response, user['username'], user['role'], pwd_ver=user.get('pwd_ver', 0))
        return response

    @app.post('/logout')
    async def logout(request: Request):
        response = redirect('/login')
        clear_auth_cookie(response)
        return response

    @app.get('/profile')
    async def profile_page(request: Request):
        return await render(
            template_name=jinja_env.get_template('profile.html'),
            context={
                'request': request,
                'is_athlete': user_is_athlete(request),
            },
        )

    @app.get('/api/profile')
    async def get_profile(request: Request):
        if get_auth_user(request) is None:
            return unauthorized(request)
        user_id = get_current_user_id(request)
        profile = get_storage(request.app).get_profile(user_id) if user_id else {}
        return json_response({'profile': profile})

    @app.post('/api/profile')
    async def save_profile(request: Request):
        if get_auth_user(request) is None:
            return unauthorized(request)
        user_id = get_current_user_id(request)
        if user_id is None:
            return text(body='Unknown user', status=400)
        profile, error = read_profile_payload(request)
        if error:
            return text(body=error, status=400)
        storage = get_storage(request.app)
        storage.set_profile(user_id, profile)
        if profile.get('student_name'):
            storage.add_name_alias(user_id, profile['student_name'])
        return json_response({'profile': profile})

    @app.get('/api/students/lookup')
    async def lookup_student(request: Request):
        # Атлету нельзя отдавать чужие ФИО — только факт точного совпадения.
        user = get_auth_user(request)
        if user is None:
            return unauthorized(request)
        if user['role'] not in MODERATOR_ROLES and user['role'] != ATHLETE_ROLE:
            return forbidden(request)

        name = str(request.args.get('name', '')).strip()
        if not name:
            return text(body='Параметр name обязателен', status=400)

        # Тот же хеш, что использует привязка записей к кабинету атлета.
        name_hash = hashlib.sha256(name.encode()).hexdigest()
        records_count = get_storage(request.app).count_records_by_student_hash(name_hash)
        return json_response({'found': records_count > 0})
