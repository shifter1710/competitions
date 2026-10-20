"""Cached admin projection. This module never performs checks or executes commands."""
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from src.backup_offsite import age_seconds
from src.ops_state import read_json

LABELS = {'OK': 'В норме', 'WARNING': 'Внимание', 'CRITICAL': 'Ошибка', 'UNKNOWN': 'Нет данных'}
TITLES = {
    'application': 'Приложение',
    'sqlite': 'SQLite',
    'local': 'Локальные копии',
    's3': 'Копии в S3',
    'restore': 'Проверка восстановления',
    'disk': 'Диск',
    'alerts': 'Telegram',
}
CODES = {
    'verified': 'Комплект проверен',
    'database_only': 'Только БД: комплект без вложений не готов к восстановлению',
    'backup_failed': 'Создание копии завершилось ошибкой',
    'sqlite_integrity_failed': 'Ошибка проверки SQLite',
    'zip_verification_failed': 'Ошибка проверки ZIP',
    'referenced_file_missing': 'В комплекте отсутствует связанный файл',
    'source_changed': 'Данные изменились при копировании',
    's3_upload_failed': 'Ошибка отправки или проверки S3',
    's3_verification_unproven': 'Проверка контрольных сумм S3 не подтверждена',
    's3_unavailable': 'S3 недоступен',
    'no_fresh_local_backup': 'Нет свежего локального комплекта',
    'disk_low': 'Недостаточно свободного места',
    'restore_failed': 'Проверка восстановления не пройдена',
    'disabled': 'Не настроен',
    'delivery_failed': 'Уведомление не доставлено',
    'backup_started': 'Создание копии выполняется',
    'unavailable': 'Проверка не пройдена',
    'ok': 'Проверка пройдена',
    'never_tested': 'NEVER TESTED',
    'pass': 'PASS',
    'synthetic_pass': 'PASS — синтетический тест',
    'artifact_integrity_failed': 'Ошибка контрольной суммы комплекта',
    'unsafe_file': 'Небезопасный файл',
}


def date_label(value):
    if age_seconds(value) is None:
        return 'Нет данных'
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')


def health_view(state_dir, *, now=None):  # noqa: C901
    # Deliberate whitelist projection: never expose arbitrary cached keys or errors.
    now = now or datetime.now(timezone.utc)
    state = read_json(Path(state_dir) / 'health.json')
    cards = []
    for key, title in TITLES.items():
        value = state.get(key, {})
        if not isinstance(value, dict):
            value = {}
        status = value.get('status', 'UNKNOWN')
        status = status if isinstance(status, str) and status in LABELS else 'UNKNOWN'
        age = age_seconds(value.get('checked_at'), now)
        code = value.get('code')
        message = CODES.get(code, 'Нет данных проверки') if isinstance(code, str) else 'Нет данных проверки'
        if age is None:
            status, message = 'UNKNOWN', 'Проверка не выполнялась или время некорректно'
        elif age > (26 * 3600 if key in {'local', 's3'} else 30 * 60):
            status, message = ('CRITICAL' if status == 'CRITICAL' else 'WARNING'), 'Данные проверки устарели'
        if key in {'local', 's3'}:
            source_age = age_seconds(value.get('created_at'), now)
            if source_age is None and status != 'CRITICAL':
                status, message = 'UNKNOWN', 'Нет подтверждённого полного комплекта'
            elif source_age is not None and source_age > 26 * 3600:
                status, message = 'CRITICAL', 'Последний подтверждённый комплект устарел'
        else:
            source_age = None
        if key == 's3':
            probe = state.get('s3_probe', {})
            probe_age = age_seconds(probe.get('checked_at'), now) if isinstance(probe, dict) else None
            if probe_age is None or probe_age > 30 * 60:
                status, message = ('CRITICAL' if status == 'CRITICAL' else 'UNKNOWN'), 'Доступность S3 не подтверждена'
            elif probe.get('status') == 'CRITICAL':
                status, message = 'CRITICAL', 'S3 недоступен'
            elif probe.get('status') != 'OK' or probe.get('code') != 'ok':
                status, message = ('CRITICAL' if status == 'CRITICAL' else 'UNKNOWN'), 'Доступность S3 не подтверждена'
        if key == 'sqlite' and status == 'OK':
            integrity_age = age_seconds(value.get('integrity_at'), now)
            if value.get('available') is not True or integrity_age is None:
                status, message = 'UNKNOWN', 'Доступность или целостность не подтверждена'
            elif integrity_age > 30 * 60:
                status, message = 'WARNING', 'Проверка целостности устарела'
        if key == 's3' and status == 'OK' and age_seconds(value.get('uploaded_at'), now) is None:
            status, message = 'UNKNOWN', 'Время подтверждённой отправки неизвестно'
        if key == 'restore':
            tested_age = age_seconds(value.get('tested_at'), now)
            if code == 'restore_failed':
                status, message = 'CRITICAL', 'FAIL'
            elif tested_age is None:
                status, message = 'UNKNOWN', 'NEVER TESTED'
            elif (
                code != 'pass'
                or value.get('scope') != 'offsite'
                or not isinstance(value.get('steps'), list)
                or not {'archives', 'references', 'isolated_start', 'http_smoke'}.issubset(
                    step for step in value.get('steps', []) if isinstance(step, str)
                )
            ):
                status, message = 'UNKNOWN', 'Полный тест восстановления не подтверждён'
            elif tested_age > 90 * 86400:
                status, message = 'WARNING', 'STALE'
        if key == 'local' and code == 'backup_started' and age is not None and age <= 30 * 60 and status == 'WARNING':
            status = 'UNKNOWN'  # Normal in-progress grace; alert only if it remains stuck.
        expected_code = {
            'application': 'ok',
            'sqlite': 'ok',
            'local': 'verified',
            's3': 'verified',
            'restore': 'pass',
            'disk': 'ok',
            'alerts': 'ok',
        }
        if status == 'OK' and code != expected_code[key]:
            status, message = 'UNKNOWN', 'Успех проверки не подтверждён'
        if key == 'disk' and status == 'OK':
            percent, free = value.get('used_percent'), value.get('free_bytes')
            if (
                type(percent) not in (int, float)
                or type(free) not in (int, float)
                or not 0 <= percent <= 100
                or free < 0
            ):
                status, message = 'UNKNOWN', 'Данные о диске отсутствуют или некорректны'
        rows = [('Последняя проверка', date_label(value.get('checked_at')))]
        if key == 'application':
            revision = value.get('revision', '')
            import re

            rows.append(
                (
                    'Ревизия',
                    revision
                    if isinstance(revision, str) and re.fullmatch('[0-9a-f]{7,40}', revision)
                    else 'Нет данных',
                )
            )
        if key == 'sqlite':
            rows += [
                ('Доступность', 'Доступна' if value.get('available') is True else 'Не подтверждена'),
                ('Целостность проверена', date_label(value.get('integrity_at'))),
            ]
        if key in {'local', 's3'}:
            rows += [
                ('Создание точки', date_label(value.get('created_at'))),
                ('Возраст точки', f'{source_age / 3600:.1f} ч' if source_age is not None else 'Нет данных'),
            ]
            if key == 'local':
                size = value.get('size')
                rows.append(
                    (
                        'Размер',
                        f'{size / 1024 / 1024:.2f} МБ'
                        if isinstance(size, (int, float)) and size >= 0
                        else 'Нет данных',
                    )
                )
            else:
                rows.append(('Отправка подтверждена', date_label(value.get('uploaded_at'))))
        if key == 'restore':
            rows.append(('Успешный полный тест', date_label(value.get('tested_at'))))
        if key == 'disk':
            for field, label in [('free_bytes', 'Свободно'), ('used_percent', 'Использовано')]:
                number = value.get(field)
                if isinstance(number, (int, float)) and number >= 0:
                    rows.append((label, f'{number / 1024**3:.2f} ГиБ' if field == 'free_bytes' else f'{number:.1f}%'))
        if key == 'alerts':
            rows.append(('Последняя доставка', date_label(value.get('last_sent_at'))))
        cards.append(
            {'key': key, 'title': title, 'status': status, 'label': LABELS[status], 'message': message, 'rows': rows}
        )
    priority = {'CRITICAL': 3, 'WARNING': 2, 'UNKNOWN': 1, 'OK': 0}
    overall = max((card['status'] for card in cards), key=priority.get)
    return {
        'cards': cards,
        'overall': overall,
        'label': LABELS[overall],
        'rendered_at': date_label(now.isoformat()),
        'issues': [card for card in cards if card['status'] != 'OK'],
    }
