"""Записи ГТО (Готов к труду и обороне) хранилища Competitions: история
ступеней и результатов студентов по годам.

ГТО принадлежит Student, а НЕ Event и не участию: identity — только
стабильный students.id (тёзки изолированы id, легаси-режим dual/ref и
ФИО-хеши к ГТО отношения не имеют). Историчность: записи никогда не
пересчитываются из возраста (DOB в модели нет), «последний статус
побеждает» без event sourcing.

Контракт примеси (Architecture v2.1): не создаёт соединение и блокировку
(self.connection / self._lock принадлежат SQLiteAdapter), не импортирует
соседние доменные модули src.storage.* (кроме src.storage.helpers),
междоменные вызовы — только через self.
"""
import sqlite3
from datetime import datetime

from src.storage.helpers import GTO_STAGE_MAX
from src.storage.helpers import GTO_STAGE_MIN
from src.storage.helpers import GTO_STATUSES
from src.storage.helpers import gto_year_max
from src.storage.helpers import GTO_YEAR_MIN


class GtoMixin:
    # ---- Записи ГТО студента ----
    #
    # Валидация здесь, а не в CHECK-ограничениях (паттерн проекта,
    # см. upsert_group_academic): год — целое 2000..текущий+1 (верхняя
    # граница вычисляется в момент вызова), ступень — целое 1..18,
    # статус — из фиксированного enum GTO_STATUSES. Отказ ничего не
    # пишет (rollback). Уникальность (student_id, year, stage) держит
    # UNIQUE-констрейнт таблицы: дубль create/правка «на занятую пару» —
    # авторитетно sqlite3.IntegrityError (паттерн add_student_alias).

    def _assert_valid_gto_fields(self, year, stage, status) -> str | None:
        """Валидация тройки (год, ступень, статус) — код ошибки или None.
        bool отсекается явно (в Python bool — подтип int)."""
        if not isinstance(year, int) or isinstance(year, bool) or not GTO_YEAR_MIN <= year <= gto_year_max():
            return 'invalid_year'
        if not isinstance(stage, int) or isinstance(stage, bool) or not GTO_STAGE_MIN <= stage <= GTO_STAGE_MAX:
            return 'invalid_stage'
        if status not in GTO_STATUSES:
            return 'invalid_status'
        return None

    def list_student_gto_records(self, student_id: int) -> list[dict]:
        """Все записи ГТО карточки: год DESC, ступень ASC, id ASC — свежие
        годы сверху, внутри года ступени по возрастанию (детерминированный
        порядок тезок-записей одной пары год+ступень невозможен — UNIQUE)."""
        with self._lock:
            rows = self.connection.execute(
                'SELECT id, student_id, year, stage, status, created_at, updated_at '
                'FROM student_gto_records WHERE student_id = ? '
                'ORDER BY year DESC, stage ASC, id ASC',
                (int(student_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_student_gto_record(self, student_id: int, record_id: int) -> dict | None:
        """Запись ГТО или None (ownership student_id — в WHERE, прецедент
        get_calendar_event_document: чужой record_id → не найдена)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT id, student_id, year, stage, status, created_at, updated_at '
                'FROM student_gto_records WHERE id = ? AND student_id = ?',
                (int(record_id), int(student_id)),
            ).fetchone()
            return dict(row) if row is not None else None

    def create_student_gto_record(
        self, student_id: int, *, year: int, stage: int, status: str
    ) -> tuple[int | None, str | None]:
        """Создать запись ГТО: (record_id, None) или (None, код ошибки).

        Коды: invalid_year / invalid_stage / invalid_status (валидация до
        обращения к БД), student_not_found, duplicate — авторитетно
        IntegrityError по UNIQUE (student_id, year, stage) с rollback;
        при любом отказе строка не создаётся.
        """
        error = self._assert_valid_gto_fields(year, stage, status)
        if error is not None:
            return None, error
        with self._lock:
            if self.get_student_by_id(int(student_id)) is None:
                return None, 'student_not_found'
            now = datetime.utcnow().isoformat()
            try:
                cursor = self.connection.execute(
                    'INSERT INTO student_gto_records (student_id, year, stage, status, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (int(student_id), year, stage, status, now, now),
                )
                self.connection.commit()
            except sqlite3.IntegrityError:
                self.connection.rollback()
                return None, 'duplicate'
            return int(cursor.lastrowid), None

    def update_student_gto_record(
        self, student_id: int, record_id: int, *, year: int, stage: int, status: str
    ) -> tuple[bool, str | None]:
        """Обновить запись ГТО (полная тройка год/ступень/статус): (True, None)
        или (False, код ошибки).

        Коды: invalid_year / invalid_stage / invalid_status / not_found
        (включая чужой record_id), duplicate — правка «на занятую пару
        (год, ступень)» по IntegrityError с rollback: исходная строка и
        строка-конфликт не меняются. updated_at записи меняется;
        students.updated_at НЕ трогается (ГТО — не правка карточки).
        """
        error = self._assert_valid_gto_fields(year, stage, status)
        if error is not None:
            return False, error
        with self._lock:
            try:
                cursor = self.connection.execute(
                    'UPDATE student_gto_records SET year = ?, stage = ?, status = ?, updated_at = ? '
                    'WHERE id = ? AND student_id = ?',
                    (year, stage, status, datetime.utcnow().isoformat(), int(record_id), int(student_id)),
                )
                if cursor.rowcount == 0:
                    self.connection.rollback()
                    return False, 'not_found'
                self.connection.commit()
            except sqlite3.IntegrityError:
                self.connection.rollback()
                return False, 'duplicate'
            return True, None

    def delete_student_gto_record(self, student_id: int, record_id: int) -> bool:
        """Удалить запись ГТО в пределах карточки (WHERE id AND student_id);
        False — записи нет (idempotent)."""
        with self._lock:
            cursor = self.connection.execute(
                'DELETE FROM student_gto_records WHERE id = ? AND student_id = ?',
                (int(record_id), int(student_id)),
            )
            self.connection.commit()
            return cursor.rowcount > 0

    def count_student_gto_records(self, student_id: int) -> int:
        """Сколько записей ГТО у карточки (блокер delete_student)."""
        with self._lock:
            row = self.connection.execute(
                'SELECT COUNT(*) AS total FROM student_gto_records WHERE student_id = ?',
                (int(student_id),),
            ).fetchone()
            return row['total']
