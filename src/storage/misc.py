"""Прочее хранилища Competitions: вложения записей, очередь конфликтов импорта, журнал аудита.

Перенесено из src/storage/sqlite.py без изменения поведения (Architecture v2.1). Контракт примеси: не
создаёт соединение и блокировку (self.connection / self._lock принадлежат SQLiteAdapter), не импортирует
соседние доменные модули src.storage.* (кроме src.storage.helpers), междоменные вызовы — только через
self."""
import json
from datetime import datetime
from typing import Sequence


class MiscMixin:
    def create_attachment(
        self,
        record_id: int,
        filename: str,
        stored_name: str,
        content_type: str,
        size: int,
        uploaded_by: int | None,
    ) -> int:
        from datetime import datetime as dt

        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO attachments (
                    record_id, filename, stored_name, content_type, size, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    filename,
                    stored_name,
                    content_type,
                    size,
                    uploaded_by,
                    dt.utcnow().isoformat(),
                ),
            )
            self.connection.commit()
            return cursor.lastrowid

    def get_attachments(self, record_id: int | None = None) -> list[dict]:
        with self._lock:
            if record_id is None:
                rows = self.connection.execute(
                    'SELECT id, record_id, filename, stored_name, content_type, size '
                    'FROM attachments ORDER BY record_id ASC, id ASC'
                ).fetchall()
            else:
                rows = self.connection.execute(
                    'SELECT id, record_id, filename, stored_name, content_type, size '
                    'FROM attachments WHERE record_id = ? ORDER BY id ASC',
                    (record_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_attachment(self, attachment_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, record_id, filename, stored_name, content_type, size ' 'FROM attachments WHERE id = ?',
                (attachment_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_attachment(self, attachment_id: int) -> None:
        with self._lock:
            self.connection.execute('DELETE FROM attachments WHERE id = ?', (attachment_id,))
            self.connection.commit()

    def count_attachments(self) -> int:
        with self._lock:
            row = self.connection.execute('SELECT COUNT(*) AS total FROM attachments').fetchone()
            return row['total']

    def delete_all_attachments(self) -> int:
        """Wipe all attachment rows (admin maintenance action). Returns deleted row count."""
        with self._lock:
            cursor = self.connection.execute('DELETE FROM attachments')
            self.connection.commit()
            return cursor.rowcount

    def get_attachments_for_records(self, record_ids: Sequence[int]) -> list[dict]:
        """Вложения указанных записей (для архива и подсчёта перед очисткой по дате)."""
        ids = [int(record_id) for record_id in record_ids]
        if not ids:
            return []
        placeholders = ', '.join('?' for _ in ids)
        with self._lock:
            rows = self.connection.execute(
                f'SELECT id, record_id, filename, stored_name, content_type, size '
                f'FROM attachments WHERE record_id IN ({placeholders}) ORDER BY record_id ASC, id ASC',
                ids,
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_attachments_for_records(self, record_ids: Sequence[int]) -> int:
        """Удалить строки вложений указанных записей (очистка по дате). Возвращает число удалённых."""
        ids = [int(record_id) for record_id in record_ids]
        if not ids:
            return 0
        placeholders = ', '.join('?' for _ in ids)
        with self._lock:
            cursor = self.connection.execute(
                f'DELETE FROM attachments WHERE record_id IN ({placeholders})',
                ids,
            )
            self.connection.commit()
            return cursor.rowcount

    def add_import_queue_entry(
        self,
        payload: dict,
        *,
        matched_record_id: int | None,
        created_by: int | None,
    ) -> int:
        """Положить конфликтную строку импорта в очередь на подтверждение."""
        with self._lock:
            cursor = self.connection.execute(
                'INSERT INTO import_queue (created_at, payload_json, status, matched_record_id, created_by) '
                "VALUES (?, ?, 'pending', ?, ?)",
                (
                    datetime.utcnow().isoformat(),
                    json.dumps(payload, ensure_ascii=False),
                    matched_record_id,
                    created_by,
                ),
            )
            self.connection.commit()
            return cursor.lastrowid

    def list_import_queue(self, status: str = 'pending') -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, created_at, payload_json, status, matched_record_id, created_by '
                'FROM import_queue WHERE status = ? ORDER BY id ASC',
                (status,),
            ).fetchall()
            entries = []
            for row in rows:
                entry = dict(row)
                entry['payload'] = json.loads(row['payload_json'])
                entries.append(entry)
            return entries

    def get_import_queue_entry(self, entry_id: int) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                'SELECT id, created_at, payload_json, status, matched_record_id, created_by '
                'FROM import_queue WHERE id = ?',
                (entry_id,),
            ).fetchone()
            if row is None:
                return None
            entry = dict(row)
            entry['payload'] = json.loads(row['payload_json'])
            return entry

    def set_import_queue_status(self, entry_id: int, status: str) -> None:
        with self._lock:
            self.connection.execute(
                'UPDATE import_queue SET status = ? WHERE id = ?',
                (status, entry_id),
            )
            self.connection.commit()

    def count_import_queue(self, status: str = 'pending') -> int:
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM import_queue WHERE status = ?',
                (status,),
            ).fetchone()
            return row['total']

    def add_audit_event(
        self,
        user_id: int | None,
        username: str,
        action: str,
        details: str = '',
    ) -> None:
        """Append a security audit event. The log is append-only by design."""
        with self._lock:
            self.connection.execute(
                'INSERT INTO audit_log (created_at, user_id, username, action, details) VALUES (?, ?, ?, ?, ?)',
                (datetime.utcnow().isoformat(), user_id, username, action, details),
            )
            self.connection.commit()

    @staticmethod
    def _audit_filter_clauses(
        username: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[str, list[object]]:
        """Общие условия фильтра журнала (даты — ISO 'YYYY-MM-DD', сравнение по DATE(created_at))."""
        clauses = []
        params: list[object] = []
        if username:
            clauses.append('username LIKE ?')
            params.append(f'%{username}%')
        if action:
            clauses.append('action = ?')
            params.append(action)
        if date_from:
            clauses.append('DATE(created_at) >= ?')
            params.append(date_from)
        if date_to:
            clauses.append('DATE(created_at) <= ?')
            params.append(date_to)
        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where, params

    def get_audit_events(
        self,
        limit: int = 200,
        offset: int = 0,
        username: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Страница журнала (новые сверху) с фильтрами и смещением пагинации."""
        with self._lock:
            where, params = self._audit_filter_clauses(username, action, date_from, date_to)
            rows = self.connection.execute(
                f'SELECT id, created_at, user_id, username, action, details '
                f'FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                (*params, int(limit), max(0, int(offset))),
            ).fetchall()
            return [dict(row) for row in rows]

    def count_audit_events(
        self,
        username: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        with self._lock:
            where, params = self._audit_filter_clauses(username, action, date_from, date_to)
            row = self.connection.execute(f'SELECT COUNT(*) AS total FROM audit_log {where}', params).fetchone()
            return row['total']

    def list_audit_actions(self) -> list[str]:
        """Различные действия журнала — для выпадающего фильтра."""
        with self._lock:
            rows = self.connection.execute('SELECT DISTINCT action FROM audit_log ORDER BY action ASC').fetchall()
            return [row['action'] for row in rows]
