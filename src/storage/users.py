"""Пользователи хранилища Competitions: аккаунты, профили и псевдонимы ФИО, привязка к карточкам, режим
идентичности dual/ref, автодополнение атлетов.

Перенесено из src/storage/sqlite.py без изменения поведения (Architecture v2.1). Контракт примеси: не
создаёт соединение и блокировку (self.connection / self._lock принадлежат SQLiteAdapter), не импортирует
соседние доменные модули src.storage.* (кроме src.storage.helpers), междоменные вызовы — только через
self."""
import hashlib
import json
import sqlite3
from datetime import datetime
from datetime import timedelta
from typing import Sequence

from src.storage.helpers import IDENTITY_MODE_DEFAULT
from src.storage.helpers import IDENTITY_MODES


class UsersMixin:
    def get_user(self, username: str) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, username, password_hash, role, active, pwd_ver, student_ref_id '
                'FROM users WHERE username = ?',
                (username,),
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, username, password_hash, role, active, pwd_ver, student_ref_id ' 'FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, username, role, active, name_aliases, last_login_at, last_seen_at '
                'FROM users ORDER BY username ASC'
            ).fetchall()
            users = []
            for row in rows:
                user = dict(row)
                user['name_aliases'] = json.loads(row['name_aliases'] or '[]')
                users.append(user)
            return users

    def create_user(self, username: str, password_hash: str, role: str) -> None:
        with self._lock:
            self.connection.execute(
                'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                (username, password_hash, role),
            )
            self.connection.commit()

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            self.connection.execute(
                '''
                UPDATE users
                SET password_hash = ?, pwd_ver = pwd_ver + 1
                WHERE id = ?
                ''',
                (password_hash, user_id),
            )
            self.connection.commit()

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE users SET active = ? WHERE id = ?',
                (int(active), user_id),
            )
            self.connection.commit()

    def set_user_last_login(self, user_id: int, when: datetime | None = None) -> None:
        """Зафиксировать успешный вход (№17): last_login_at и last_seen_at.

        Вход — тоже активность, поэтому last_seen обновляется вместе с входом;
        история входов остаётся и в аудите (login_success).
        """
        with self._lock:
            timestamp = (when or datetime.utcnow()).isoformat()
            self.connection.execute(
                'UPDATE users SET last_login_at = ?, last_seen_at = ? WHERE id = ?',
                (timestamp, timestamp, user_id),
            )
            self.connection.commit()

    def touch_user_seen(self, user_id: int, throttle_seconds: int = 60) -> bool:
        """Обновить last_seen_at, если он старше throttle_seconds (№17).

        Троттлинг в самом UPDATE — без отдельного чтения: условие WHERE не
        пропустит запись чаще раза в окно, поэтому «два запроса подряд» дают
        один UPDATE. Возвращает, была ли строка обновлена.
        """
        now = datetime.utcnow()
        cutoff = (now - timedelta(seconds=throttle_seconds)).isoformat()
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE users SET last_seen_at = ? ' 'WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)',
                (now.isoformat(), user_id, cutoff),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def count_records_by_owner(self, owner_id: int) -> int:
        """Сколько записей соревнований привязано к аккаунту владельца."""
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM competitions WHERE owner_id = ?',
                (owner_id,),
            ).fetchone()
            return row['total']

    def delete_user(self, user_id: int) -> int:
        """Удалить аккаунт, сохранив записи как исторические факты.

        Записи не удаляются: owner_id обнуляется, строки остаются в таблице,
        отчётах и выгрузках. Псевдонимы ФИО живут в строке пользователя
        (users.name_aliases) и исчезают вместе с аккаунтом. Аудит-журнал не
        трогается (append-only, события хранят username текстом). См.
        docs/data-model-decisions.md «Удаление пользователей». Возвращает
        число отвязанных записей.
        """
        with self._lock:
            cursor = self.connection.execute(
                'UPDATE competitions SET owner_id = NULL WHERE owner_id = ?',
                (user_id,),
            )
            detached = cursor.rowcount
            self.connection.execute('DELETE FROM users WHERE id = ?', (user_id,))
            self.connection.commit()
            return detached

    def get_profile(self, user_id: int) -> dict:
        with self._lock:
            row = self.connection.execute(
                'SELECT profile_data FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            if row is None:
                return {}
            return json.loads(row['profile_data'] or '{}')

    def set_profile(self, user_id: int, profile: dict) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE users SET profile_data = ? WHERE id = ?',
                (json.dumps(profile, ensure_ascii=False), user_id),
            )
            self.connection.commit()

    def get_name_aliases(self, user_id: int) -> list[str]:
        with self._lock:
            row = self.connection.execute(
                'SELECT name_aliases FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            if row is None:
                return []
            return json.loads(row['name_aliases'] or '[]')

    def add_name_alias(self, user_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            return
        with self._lock:
            row = self.connection.execute(
                'SELECT name_aliases FROM users WHERE id = ?',
                (user_id,),
            ).fetchone()
            if row is None:
                return
            aliases = json.loads(row['name_aliases'] or '[]')
            if name not in aliases:
                aliases.append(name)
                self.connection.execute(
                    'UPDATE users SET name_aliases = ? WHERE id = ?',
                    (json.dumps(aliases, ensure_ascii=False), user_id),
                )
                self.connection.commit()

    def carry_name_aliases(self, from_name: str, to_name: str) -> int:
        """After a student merge, add `to_name` to accounts that have `from_name` alias.

        Keeps athlete dashboards intact: records rewritten to the new name hash
        stay visible via the new alias. `from_name` is never removed
        (aliases list only grows). Returns the number of updated accounts.
        """
        from_name = from_name.strip()
        to_name = to_name.strip()
        if not from_name or not to_name or from_name == to_name:
            return 0
        with self._lock:
            rows = self.connection.execute('SELECT id, name_aliases FROM users').fetchall()
            updated = 0
            for row in rows:
                aliases = json.loads(row['name_aliases'] or '[]')
                if from_name in aliases and to_name not in aliases:
                    aliases.append(to_name)
                    self.connection.execute(
                        'UPDATE users SET name_aliases = ? WHERE id = ?',
                        (json.dumps(aliases, ensure_ascii=False), row['id']),
                    )
                    updated += 1
            if updated:
                self.connection.commit()
            return updated

    def link_user(self, user_id: int, student_id: int) -> tuple[int, str | None]:
        """Привязать аккаунт атлета к карточке. Два аккаунта МОГУТ указывать
        на одну карточку (дубликаты — вопрос модерации, не запрета).

        Коды ошибок: user_not_found / user_not_athlete / user_already_linked /
        student_not_found / student_inactive.
        """
        with self._lock:
            user = self.connection.execute(
                'SELECT role, student_ref_id FROM users WHERE id = ?',
                (int(user_id),),
            ).fetchone()
            if user is None:
                return 0, 'user_not_found'
            if user['role'] != 'athlete':
                return 0, 'user_not_athlete'
            if user['student_ref_id'] is not None:
                return 0, 'user_already_linked'
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return 0, 'student_not_found'
            if not student['active']:
                return 0, 'student_inactive'
            cursor = self.connection.execute(
                'UPDATE users SET student_ref_id = ? WHERE id = ? AND student_ref_id IS NULL',
                (int(student_id), int(user_id)),
            )
            self.connection.commit()
            return cursor.rowcount, None

    def unlink_user(self, user_id: int) -> int | None:
        """Снять связь аккаунта с карточкой; прежний student_ref_id (или
        None, если аккаунта нет либо связи и так не было)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT student_ref_id FROM users WHERE id = ?',
                (int(user_id),),
            ).fetchone()
            if row is None or row['student_ref_id'] is None:
                return None
            self.connection.execute('UPDATE users SET student_ref_id = NULL WHERE id = ?', (int(user_id),))
            self.connection.commit()
            return row['student_ref_id']

    def relink_user(self, user_id: int, student_id: int) -> tuple[int | None, str | None]:
        """Перепривязать аккаунт атлета на другую карточку (можно и с NULL).
        Возвращает (прежний ref или None, ошибка или None)."""
        with self._lock:
            user = self.connection.execute(
                'SELECT role, student_ref_id FROM users WHERE id = ?',
                (int(user_id),),
            ).fetchone()
            if user is None:
                return None, 'user_not_found'
            if user['role'] != 'athlete':
                return None, 'user_not_athlete'
            student = self.connection.execute(
                'SELECT active FROM students WHERE id = ?',
                (int(student_id),),
            ).fetchone()
            if student is None:
                return None, 'student_not_found'
            if not student['active']:
                return None, 'student_inactive'
            self.connection.execute(
                'UPDATE users SET student_ref_id = ? WHERE id = ?',
                (int(student_id), int(user_id)),
            )
            self.connection.commit()
            return user['student_ref_id'], None

    def linked_athlete_users(self, student_id: int) -> list[dict]:
        """Аккаунты атлетов, привязанные к карточке, по логину."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, username, active FROM users '
                "WHERE role = 'athlete' AND student_ref_id = ? ORDER BY username ASC",
                (int(student_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_unlinked_athlete_users(self) -> list[dict]:
        """Аккаунты атлетов без карточки студента, по логину.

        Без пагинации: аккаунтов немного, а карточки развёрнутые.
        """
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, username, active, profile_data, name_aliases FROM users '
                "WHERE role = 'athlete' AND student_ref_id IS NULL ORDER BY username ASC"
            ).fetchall()
            return [
                {
                    'id': row['id'],
                    'username': row['username'],
                    'active': row['active'],
                    'profile_data': json.loads(row['profile_data'] or '{}'),
                    'name_aliases': json.loads(row['name_aliases'] or '[]'),
                }
                for row in rows
            ]

    def get_unlinked_athlete_user(self, user_id: int) -> dict | None:
        """Один аккаунт атлета без карточки по id (для создания студента
        из профиля): None — нет такого, роль не athlete или уже привязан."""
        with self._lock:
            row = self.connection.execute(
                'SELECT id, username, active, profile_data, name_aliases FROM users '
                "WHERE id = ? AND role = 'athlete' AND student_ref_id IS NULL",
                (int(user_id),),
            ).fetchone()
            if row is None:
                return None
            return {
                'id': row['id'],
                'username': row['username'],
                'active': row['active'],
                'profile_data': json.loads(row['profile_data'] or '{}'),
                'name_aliases': json.loads(row['name_aliases'] or '[]'),
            }

    # ---- P3 Runtime Identity: режим dual/ref и проверка перед переходом. ----
    #
    # Кабинет атлета с этой фазы читает стабильные связи student_ref_id
    # (dual: owner OR ref OR легаси-хеш ФИО; ref: owner OR ref). Режим —
    # флаг app_settings.identity_mode, читается на каждый запрос без кэша.
    # Схема не меняется; легаси-ключ student_id и история aliases живут
    # как раньше. Переход на 'ref' — только через set_identity_mode_guarded:
    # guard без waiver блокирует переключение, пока хоть одна запись видна
    # атлету ТОЛЬКО по легаси-хешу.

    def get_identity_mode(self) -> str:
        """Текущий режим идентичности (per-request, без кэша). Нет строки или
        неизвестное значение → 'dual': легаси-поведение безопаснее."""
        with self._lock:
            row = self.connection.execute("SELECT value FROM app_settings WHERE key = 'identity_mode'").fetchone()
        if row and row['value'] in IDENTITY_MODES:
            return row['value']
        return IDENTITY_MODE_DEFAULT

    def set_identity_mode(self, value: str) -> None:
        """Прямая установка режима (валидация ∈ {dual, ref}), без guard.
        Рабочий путь переключения — set_identity_mode_guarded."""
        if value not in IDENTITY_MODES:
            raise ValueError(f'Unknown identity mode: {value}')
        with self._lock:
            self.connection.execute(
                "INSERT INTO app_settings (key, value) VALUES ('identity_mode', ?) "
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (value,),
            )
            self.connection.commit()

    def athlete_name_hashes(self, user_id: int) -> list[str]:
        """Легаси-хеши ФИО аккаунта: profile_data.student_name и все
        name_aliases — та же формула, что student_hashes_for_request
        в src/main.py (sha256(strip(name))). Нужен storage-у для отчётов
        по произвольным аккаунтам, а не только по текущему запросу."""
        with self._lock:
            row = self.connection.execute(
                'SELECT profile_data, name_aliases FROM users WHERE id = ?',
                (int(user_id),),
            ).fetchone()
        if row is None:
            return []
        names = list(json.loads(row['name_aliases'] or '[]'))
        profile_name = json.loads(row['profile_data'] or '{}').get('student_name', '').strip()
        if profile_name and profile_name not in names:
            names.append(profile_name)
        return [hashlib.sha256(name.strip().encode()).hexdigest() for name in names if name.strip()]

    def count_competitions_visible(
        self,
        *,
        owner_id: int | None,
        student_ref_id: int | None = None,
        student_id_hashes: Sequence[str] = (),
        identity_mode: str = IDENTITY_MODE_DEFAULT,
    ) -> int:
        """Сколько записей видно атлету с данным scope (без прочих фильтров)
        — предпросмотры «сейчас N · после привязки/отвязки M»."""
        with self._lock:
            clauses, params = self._competition_scope_clauses(
                owner_id, student_id_hashes, student_ref_id=student_ref_id, identity_mode=identity_mode
            )
            where_clause = f'WHERE {" AND ".join(clauses)}' if clauses else ''
            row = self.connection.execute(
                f'SELECT COUNT(*) FROM competitions {where_clause}',
                params,
            ).fetchone()
            return int(row[0])

    @staticmethod
    def _hash_only_where(
        user_id: int,
        student_ref_id: int | None,
        hashes: Sequence[str],
    ) -> tuple[str, list[object]]:
        """WHERE записей, видимых атлету ТОЛЬКО по легаси-хешу ФИО: хеш
        совпадает, но записью не владеет и на его карточку она не связана.
        Такие записи пропадают из кабинета в ref-режиме."""
        clauses = ['(owner_id IS NULL OR owner_id != ?)']
        params: list[object] = [int(user_id)]
        if student_ref_id is not None:
            clauses.append('(student_ref_id IS NULL OR student_ref_id != ?)')
            params.append(int(student_ref_id))
        placeholders = ', '.join('?' for _ in hashes)
        clauses.append(f'student_id IN ({placeholders})')
        params.extend(hashes)
        return ' AND '.join(clauses), params

    def _identity_user_rows(self) -> list[dict]:
        """Атлеты с их scope-числами (без пагинации — режет вызывающая
        сторона). Считается под уже взятым self._lock."""
        users = self.connection.execute(
            'SELECT id, username, student_ref_id FROM users ' "WHERE role = 'athlete' ORDER BY username ASC"
        ).fetchall()
        cards = {
            row['id']: row['full_name']
            for row in self.connection.execute('SELECT id, full_name FROM students').fetchall()
        }
        rows: list[dict] = []
        for user in users:
            user_id = user['id']
            ref = user['student_ref_id']
            hashes = self.athlete_name_hashes(user_id)
            row = {
                'user_id': user_id,
                'username': user['username'],
                'student_ref_id': ref,
                'student_name': cards.get(ref) if ref is not None else None,
                'visible_by_owner': self.connection.execute(
                    'SELECT COUNT(*) AS total FROM competitions WHERE owner_id = ?',
                    (user_id,),
                ).fetchone()['total'],
                'visible_by_ref': (
                    self.connection.execute(
                        'SELECT COUNT(*) AS total FROM competitions WHERE student_ref_id = ?',
                        (ref,),
                    ).fetchone()['total']
                    if ref is not None
                    else 0
                ),
                'hash_only_visible': 0,
                'visible_by_hash': 0,
                'namesake_risk': 0,
            }
            if hashes:
                placeholders = ', '.join('?' for _ in hashes)
                hash_where = f'student_id IN ({placeholders})'
                row['visible_by_hash'] = self.connection.execute(
                    f'SELECT COUNT(*) AS total FROM competitions WHERE {hash_where}',
                    list(hashes),
                ).fetchone()['total']
                hash_only_where, hash_only_params = self._hash_only_where(user_id, ref, hashes)
                row['hash_only_visible'] = self.connection.execute(
                    f'SELECT COUNT(*) AS total FROM competitions WHERE {hash_only_where}',
                    hash_only_params,
                ).fetchone()['total']
                # Риск тёзки: запись совпала с ФИО аккаунта, но привязана
                # к ДРУГОЙ карточке — легаси показывает её в кабинете.
                namesake_clauses = [f'student_id IN ({placeholders})', 'student_ref_id IS NOT NULL']
                namesake_params = list(hashes)
                if ref is not None:
                    namesake_clauses.append('student_ref_id != ?')
                    namesake_params.append(int(ref))
                row['namesake_risk'] = self.connection.execute(
                    f"SELECT COUNT(*) AS total FROM competitions WHERE {' AND '.join(namesake_clauses)}",
                    namesake_params,
                ).fetchone()['total']
            rows.append(row)
        return rows

    def count_hash_only_visible(self) -> int:
        """Guard-счётчик: суммарно записей, видимых атлетам только по
        легаси-хешу (по всем аккаунтам athlete; одна запись, совпавшая
        с двумя аккаунтами, считается дважды — каждый кабинет потеряет её)."""
        with self._lock:
            return sum(row['hash_only_visible'] for row in self._identity_user_rows())

    def identity_verification_data(
        self,
        users_limit: int = 50,
        users_offset: int = 0,
        disappearing_limit: int = 20,
    ) -> dict:
        """Данные отчёта «Режим идентификации» (/admin/people/reconcile/identity).

        Возвращает per-user строки (пагинация users_limit/users_offset —
        паттерн сопоставления), глобальный hash_only_total (бейдж вкладки и
        guard) и первые disappearing_limit записей, которые пропадут из
        кабинетов при переходе на ref (с аккаунтом, которому пропадут).
        """
        with self._lock:
            rows = self._identity_user_rows()
            disappearing: list[dict] = []
            for row in rows:
                if not row['hash_only_visible']:
                    continue
                hashes = self.athlete_name_hashes(row['user_id'])
                hash_only_where, hash_only_params = self._hash_only_where(row['user_id'], row['student_ref_id'], hashes)
                records = self.connection.execute(
                    f'''
                    SELECT id, student_name, sport, date, date_to, name
                    FROM competitions
                    WHERE {hash_only_where}
                    ORDER BY date ASC, id ASC
                    LIMIT ?
                    ''',
                    [*hash_only_params, int(disappearing_limit)],
                ).fetchall()
                for record in records:
                    disappearing.append({'username': row['username'], **dict(record)})
            disappearing.sort(key=lambda item: (item['username'], item['date'], item['id']))
            total = sum(row['hash_only_visible'] for row in rows)
            users_start = int(users_offset)
            users_end = users_start + int(users_limit)
            return {
                'users_total': len(rows),
                'users': rows[users_start:users_end],
                'hash_only_total': total,
                'disappearing': disappearing[: int(disappearing_limit)],
                'disappearing_total': total,
            }

    def set_identity_mode_guarded(self, target: str) -> tuple[bool, int]:
        """Атомарное переключение режима с guard'ом (без waiver).

        target='dual' — всегда разрешён (возврат легаси-поведения).
        target='ref' — только при hash_only_total == 0: иначе режим НЕ
        меняется. Проверка и UPDATE — под одной блокировкой, чтобы между
        ними никто не привязал/отвязал записи. Возвращает (ok, счётчик
        hash-only на момент попытки — для аудита/отказа)."""
        if target not in IDENTITY_MODES:
            raise ValueError(f'Unknown identity mode: {target}')
        with self._lock:
            count = sum(row['hash_only_visible'] for row in self._identity_user_rows())
            if target == 'ref' and count >= 1:
                return False, count
            self.connection.execute(
                "INSERT INTO app_settings (key, value) VALUES ('identity_mode', ?) "
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (target,),
            )
            self.connection.commit()
            return True, count

    def search_athletes(self, query: str, limit: int = 8) -> list[dict]:
        """№23доп (docs/feedback-live.md): варианты атлетов по подстроке ФИО.

        Источник — объединение профилей (users.profile_data) и ПОСЛЕДНЕЙ
        записи с таким ФИО. Сравнение регистра — в Python: lower() в SQLite
        не приводит кириллицу (тот же подход, что в find_catalog_canonical).
        Возвращает до `limit` вариантов, отсортированных по алфавиту; поля
        пустые значения не включает.
        """
        query = query.strip().lower()
        if not query:
            return []
        athletes = self._known_athletes()
        matches = [entry for name, entry in athletes.items() if query in name.lower()]
        matches.sort(key=lambda entry: entry['name'].lower())
        return matches[:limit]

    def find_athlete_fields(self, name: str) -> dict:
        """№23доп: известные поля атлета по ТОЧНОМУ ФИО (без учёта регистра).

        Тот же источник, что search_athletes. Пустых значений в ответе нет:
        вызывающая сторона (автозаполнение импорта) подставляет только
        непустые поля и не перезаписывает заполненные.
        """
        name = name.strip().lower()
        if not name:
            return {}
        return self._known_athletes().get(name, {})

    _ATHLETE_FIELD_MAP: tuple[tuple[str, str], ...] = (
        ('student_sex', 'sex'),
        ('institute', 'institute'),
        ('group', 'group'),
        ('course', 'course'),
    )

    @classmethod
    def _athlete_entry_from_record(cls, row: sqlite3.Row) -> dict:
        entry = {'name': row['student_name']}
        for source_key, result_key in cls._ATHLETE_FIELD_MAP:
            value = row[source_key]
            if value is not None and str(value).strip():
                entry[result_key] = str(value).strip()
        return entry

    @classmethod
    def _athlete_entry_from_profile(cls, profile_data: str) -> dict | None:
        try:
            profile = json.loads(profile_data or '{}')
        except (TypeError, ValueError):
            return None
        if not isinstance(profile, dict):
            return None
        entry = {'name': str(profile.get('student_name') or '').strip()}
        if not entry['name']:
            return None
        for source_key, result_key in cls._ATHLETE_FIELD_MAP:
            value = profile.get(source_key)
            if value is not None and str(value).strip():
                entry[result_key] = str(value).strip()
        return entry

    def _known_athletes(self) -> dict[str, dict]:
        """Атлеты по ФИО: профиль приоритетнее, пробелы добирает последняя запись.

        Профили обходятся первыми (сознательно заполненные данные), затем
        записи от последней к первой: первое встреченное непустое значение
        записи — из последней записи. Ничего не перезаписывается: слияние
        только дозаполняет пробелы (_merge_athlete_entry).
        """
        merged: dict[str, dict] = {}
        with self._lock:
            profile_rows = self.connection.execute('SELECT profile_data FROM users').fetchall()
            # Последняя запись с таким ФИО: created_at DESC; id DESC —
            # тай-брейк для записей одной секунды (импорт).
            record_rows = self.connection.execute(
                'SELECT student_name, student_sex, institute, "group", course '
                'FROM competitions ORDER BY created_at DESC, id DESC'
            ).fetchall()
        for row in profile_rows:
            entry = self._athlete_entry_from_profile(row['profile_data'])
            if entry is not None:
                self._merge_athlete_entry(merged, entry)
        for row in record_rows:
            self._merge_athlete_entry(merged, self._athlete_entry_from_record(row))
        return merged

    @staticmethod
    def _merge_athlete_entry(merged: dict[str, dict], entry: dict) -> None:
        """Дозаполнить вариант атлета первыми встреченными непустыми значениями."""
        key = entry['name'].lower()
        target = merged.setdefault(key, {'name': entry['name']})
        for result_key in ('sex', 'institute', 'group', 'course'):
            if result_key not in target and result_key in entry:
                target[result_key] = entry[result_key]
