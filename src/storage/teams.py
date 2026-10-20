"""Event teams and explicit participation membership. No inferred identities."""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from src.storage.helpers import dedup_discipline


team_actor = ContextVar('team_actor', default=None)
team_removed = ContextVar('team_removed', default=0)


class TeamError(ValueError):
    pass


def effective_discipline(row, fields):
    # records imports SQLiteAdapter; defer this shared semantics import until runtime.
    from src.records import event_participation_effective_values

    return event_participation_effective_values(row, fields)[0]


class TeamsMixin:
    def _migrate_teams(self):
        """One additive savepoint, no roster or historical data backfill."""
        self.connection.execute('SAVEPOINT team_schema')
        try:
            statements = [
                '''CREATE TABLE IF NOT EXISTS calendar_event_teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calendar_event_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)''',
                '''CREATE INDEX IF NOT EXISTS idx_event_teams_event
                   ON calendar_event_teams(calendar_event_id, id)''',
                '''CREATE TABLE IF NOT EXISTS calendar_event_team_members (
                    team_id INTEGER NOT NULL,
                    competition_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(team_id, competition_id))''',
                '''CREATE INDEX IF NOT EXISTS idx_team_members_competition
                   ON calendar_event_team_members(competition_id, team_id)''',
                '''CREATE TABLE IF NOT EXISTS calendar_event_team_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calendar_event_id INTEGER NOT NULL,
                    team_id INTEGER NOT NULL,
                    team_name TEXT NOT NULL,
                    competition_id INTEGER,
                    student_ref_id INTEGER,
                    student_name TEXT,
                    discipline TEXT,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor_id INTEGER,
                    actor_name TEXT,
                    created_at TEXT NOT NULL)''',
                '''CREATE INDEX IF NOT EXISTS idx_team_history_event
                   ON calendar_event_team_history(calendar_event_id, team_id, id)''',
            ]
            for statement in statements:
                self.connection.execute(statement)
            for table in ('calendar_event_documents', 'calendar_event_team_results'):
                columns = {row['name'] for row in self.connection.execute(f'PRAGMA table_info({table})')}
                if 'team_id' not in columns:
                    self.connection.execute(f'ALTER TABLE {table} ADD COLUMN team_id INTEGER')
                self.connection.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_team ON {table}(team_id)')
            self.connection.execute('RELEASE SAVEPOINT team_schema')
        except BaseException:
            self.connection.execute('ROLLBACK TO SAVEPOINT team_schema')
            self.connection.execute('RELEASE SAVEPOINT team_schema')
            raise

    @contextmanager
    def _team_transaction(self):
        """SQLite write reservation covers checks and writes on every connection."""
        with self._lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def _team_snapshot(self):
        fields = self.get_custom_fields()
        rows = self.connection.execute(
            '''SELECT t.id AS team_id, t.name AS team_name, t.calendar_event_id,
                      c.id AS competition_id, c.calendar_event_id AS participation_event_id,
                      c.student_ref_id, c.student_name,
                      c.discipline, c.extra_data
               FROM calendar_event_team_members m
               JOIN calendar_event_teams t ON t.id = m.team_id
               JOIN competitions c ON c.id = m.competition_id'''
        ).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            row['extra_data'] = json.loads(row['extra_data'] or '{}')
            row['discipline'] = effective_discipline(row, fields)
            row.pop('extra_data')
            result.append(row)
        return result

    @contextmanager
    def _team_participation_write(self):
        """Wrap existing participation mutations; helpers never commit themselves.

        Only current memberships are snapshotted. Deleted/changed participation
        history is written before the same commit, including bulk cleanup paths.
        This is intentionally not a general storage transaction framework.
        """
        removed = 0
        with self._team_transaction():
            before = self._team_snapshot()
            yield
            if not before or not getattr(self.connection, 'in_transaction', True):
                return  # An existing guarded mutation explicitly rolled back.
            fields = self.get_custom_fields()
            for snapshot in before:
                row = self.connection.execute(
                    'SELECT * FROM competitions WHERE id = ?', (snapshot['competition_id'],)
                ).fetchone()
                current = dict(row) if row else None
                reason = ''
                if current is None:
                    reason = 'participation_deleted'
                elif current['calendar_event_id'] != snapshot['participation_event_id']:
                    reason = 'event_changed'
                else:
                    current['extra_data'] = json.loads(current['extra_data'] or '{}')
                    discipline = effective_discipline(current, fields)
                    if dedup_discipline(discipline) != dedup_discipline(snapshot['discipline']):
                        reason = 'discipline_changed'
                if reason:
                    removed += int(reason == 'discipline_changed')
                    self._team_history(snapshot, 'removed', reason)
                    self.connection.execute(
                        'DELETE FROM calendar_event_team_members WHERE team_id = ? AND competition_id = ?',
                        (snapshot['team_id'], snapshot['competition_id']),
                    )
                elif current['student_ref_id'] != snapshot['student_ref_id']:
                    self._team_history(
                        snapshot,
                        'identity_changed',
                        f"student_ref:{snapshot['student_ref_id']}->{current['student_ref_id']}",
                    )

        team_removed.set(team_removed.get() + removed)

    def _team_history(self, snapshot, action, reason='', actor=None):
        actor = actor or team_actor.get() or {}
        self.connection.execute(
            '''INSERT INTO calendar_event_team_history
               (calendar_event_id, team_id, team_name, competition_id, student_ref_id,
                student_name, discipline, action, reason, actor_id, actor_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                snapshot['calendar_event_id'],
                snapshot['team_id'],
                snapshot['team_name'],
                snapshot.get('competition_id'),
                snapshot.get('student_ref_id'),
                snapshot.get('student_name'),
                snapshot.get('discipline'),
                action,
                reason,
                actor.get('id'),
                actor.get('username'),
                datetime.utcnow().isoformat(),
            ),
        )

    def list_event_teams(self, event_id):
        with self._lock:
            return [
                dict(row)
                for row in self.connection.execute(
                    '''SELECT t.*, (SELECT COUNT(*) FROM calendar_event_team_members m
                                 JOIN competitions c ON c.id=m.competition_id
                                 WHERE m.team_id=t.id AND c.calendar_event_id=t.calendar_event_id) AS member_count
                   FROM calendar_event_teams t WHERE t.calendar_event_id=? ORDER BY t.id''',
                    (int(event_id),),
                )
            ]

    def get_event_team(self, event_id, team_id):
        with self._lock:
            row = self.connection.execute(
                'SELECT * FROM calendar_event_teams WHERE id=? AND calendar_event_id=?', (int(team_id), int(event_id))
            ).fetchone()
            return dict(row) if row else None

    def _require_team(self, event_id, team_id):
        team = self.get_event_team(event_id, team_id)
        if team is None:
            raise TeamError('Команда не найдена в этом соревновании.')
        return team

    @staticmethod
    def _team_name(name):
        name = str(name or '').strip()
        if not name or len(name) > 200:
            raise TeamError('Укажите название команды: от 1 до 200 символов.')
        return name

    def create_event_team(self, event_id, name, actor=None):
        name = self._team_name(name)
        with self._team_transaction():
            if self.connection.execute('SELECT 1 FROM calendar_events WHERE id=?', (event_id,)).fetchone() is None:
                raise TeamError('Соревнование не найдено.')
            now = datetime.utcnow().isoformat()
            cursor = self.connection.execute(
                'INSERT INTO calendar_event_teams ' '(calendar_event_id,name,created_at,updated_at) VALUES (?,?,?,?)',
                (event_id, name, now, now),
            )
            team_id = cursor.lastrowid
            self._team_history(
                dict(calendar_event_id=event_id, team_id=team_id, team_name=name), 'created', actor=actor
            )
            return team_id

    def rename_event_team(self, event_id, team_id, name, actor=None):
        name = self._team_name(name)
        with self._team_transaction():
            team = self._require_team(event_id, team_id)
            if team['name'] != name:
                self._team_history(
                    dict(calendar_event_id=event_id, team_id=team_id, team_name=team['name']), 'renamed', name, actor
                )
                self.connection.execute(
                    'UPDATE calendar_event_teams SET name=?,updated_at=? WHERE id=?',
                    (name, datetime.utcnow().isoformat(), team_id),
                )

    def delete_event_team(self, event_id, team_id, actor=None):
        with self._team_transaction():
            team = self._require_team(event_id, team_id)
            for table in ('calendar_event_team_members', 'calendar_event_documents', 'calendar_event_team_results'):
                if self.connection.execute(f'SELECT 1 FROM {table} WHERE team_id=? LIMIT 1', (team_id,)).fetchone():
                    raise TeamError('Команда содержит участников, документы или результаты. Сначала удалите связи.')
            self._team_history(
                dict(calendar_event_id=event_id, team_id=team_id, team_name=team['name']), 'deleted', actor=actor
            )
            self.connection.execute('DELETE FROM calendar_event_teams WHERE id=?', (team_id,))

    def change_event_team_members(self, event_id, team_id, competition_ids, action, actor=None):
        if action not in ('add', 'remove'):
            raise TeamError('Неизвестное действие с составом.')
        try:
            ids = sorted({int(value) for value in competition_ids})
        except (ValueError, TypeError):
            raise TeamError('Некорректный номер участия.') from None
        if not ids:
            raise TeamError('Выберите хотя бы одно участие.')
        with self._team_transaction():
            team = self._require_team(event_id, team_id)
            rows = []
            for record_id in ids:
                row = self.connection.execute(
                    'SELECT * FROM competitions WHERE id=? AND calendar_event_id=?', (record_id, event_id)
                ).fetchone()
                if row is None:
                    raise TeamError(
                        'Выбранное участие удалено или относится к другому соревнованию. Состав не изменён.'
                    )
                rows.append(dict(row))
            fields = self.get_custom_fields()
            changed = 0
            for row in rows:
                if action == 'add':
                    cursor = self.connection.execute(
                        'INSERT OR IGNORE INTO calendar_event_team_members '
                        '(team_id,competition_id,created_at) VALUES (?,?,?)',
                        (team_id, row['id'], datetime.utcnow().isoformat()),
                    )
                else:
                    cursor = self.connection.execute(
                        'DELETE FROM calendar_event_team_members ' 'WHERE team_id=? AND competition_id=?',
                        (team_id, row['id']),
                    )
                if cursor.rowcount:
                    row['extra_data'] = json.loads(row['extra_data'] or '{}')
                    snapshot = dict(
                        calendar_event_id=event_id,
                        team_id=team_id,
                        team_name=team['name'],
                        competition_id=row['id'],
                        student_ref_id=row['student_ref_id'],
                        student_name=row['student_name'],
                        discipline=effective_discipline(row, fields),
                    )
                    self._team_history(snapshot, 'added' if action == 'add' else 'removed', 'manual', actor)
                    changed += 1
            return changed

    def event_team_member_ids(self, event_id, team_id):
        with self._lock:
            return {
                row[0]
                for row in self.connection.execute(
                    '''SELECT c.id FROM calendar_event_team_members m
                   JOIN competitions c ON c.id=m.competition_id
                   JOIN calendar_event_teams t ON t.id=m.team_id
                   WHERE t.id=? AND t.calendar_event_id=? AND c.calendar_event_id=t.calendar_event_id''',
                    (team_id, event_id),
                )
            }

    def event_team_history(self, event_id, team_id=None):
        with self._lock:
            where, params = 'calendar_event_id=?', [event_id]
            if team_id is not None:
                where += ' AND team_id=?'
                params.append(team_id)
            return [
                dict(row)
                for row in self.connection.execute(
                    f'SELECT * FROM calendar_event_team_history WHERE {where} ORDER BY id DESC', params
                )
            ]
