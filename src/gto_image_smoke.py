"""Opt-in synthetic-only exact-image GTO migration inventory for CI.

Never run against an existing application DB. seed-legacy requires a missing
DB and explicit GTO_SYNTHETIC_IMAGE_SMOKE=1. Inventory contains fictional data.
"""
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from src.settings import settings
from src.storage.sqlite import SQLiteAdapter


def main():
    if os.environ.get('GTO_SYNTHETIC_IMAGE_SMOKE') != '1':
        raise RuntimeError('Synthetic smoke requires explicit opt-in')
    root = Path(settings.data_folder)
    database = Path(settings.database_path)
    inventory_path = root / 'gto-synthetic-inventory.json'
    if sys.argv[1:] == ['seed-legacy']:
        if database.exists() or inventory_path.exists():
            raise RuntimeError('Synthetic smoke refuses an existing database')
        root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.execute(
            'CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL, '
            'sex TEXT, institute TEXT, group_name TEXT, course TEXT, active INTEGER NOT NULL DEFAULT 1, '
            'merged_into_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)'
        )
        connection.execute(
            "INSERT INTO students VALUES (1, 'Синтетический Студент CI', '', 'Демо', 'ДЕМО-26', NULL, "
            "1, NULL, 'synthetic-created', 'synthetic-updated')"
        )
        connection.execute(
            'CREATE TABLE student_gto_records ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, year INTEGER NOT NULL, '
            'stage INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
            'UNIQUE(student_id, year, stage))'
        )
        old = []
        for index, status in enumerate(('registered', 'participated', 'bronze', 'silver', 'gold')):
            row = [index + 20, 1, 2021 + index, 8, status, 'synthetic-created', 'synthetic-updated']
            connection.execute('INSERT INTO student_gto_records VALUES (?, ?, ?, ?, ?, ?, ?)', row)
            old.append(row)
        connection.execute("INSERT INTO student_gto_records VALUES (999, 1, 2026, 8, 'gold', 'x', 'x')")
        connection.execute('DELETE FROM student_gto_records WHERE id=999')
        connection.commit()
        connection.close()
        # An unrelated historical document file must remain byte-for-byte intact.
        historical_file = root / 'files' / 'calendar' / '17' / ('a' * 32 + '.pdf')
        historical_file.parent.mkdir(parents=True)
        historical_file.write_bytes(b'%PDF-1.4\nSynthetic historical document\n')
        inventory_path.write_text(
            json.dumps(
                {
                    'old_rows': old,
                    'file': str(historical_file.relative_to(root)),
                    'sha256': hashlib.sha256(historical_file.read_bytes()).hexdigest(),
                }
            )
        )
        print('Synthetic legacy inventory: 5 facts; 3 awards; high-water 999; 1 file')
    elif sys.argv[1:] == ['verify']:
        inventory = json.loads(inventory_path.read_text())
        adapter = SQLiteAdapter(str(database))
        try:
            rows = {r['id']: r for r in adapter.list_student_gto_records(1)}
            fields = ('id', 'student_id', 'year', 'stage', 'status', 'created_at', 'updated_at')
            for old in inventory['old_rows']:
                assert [rows[old[0]][field] for field in fields] == old
                assert rows[old[0]]['awarded_on'] is None
            assert hashlib.sha256((root / inventory['file']).read_bytes()).hexdigest() == inventory['sha256']
            if 'new_award' not in inventory:
                new, error = adapter.create_student_gto_record(1, year=2026, stage=8, status='gold')
                assert error is None and new > 999
                inventory['new_award'] = new
                inventory_path.write_text(json.dumps(inventory))
            assert adapter.count_student_gto_records(1) == 6
            assert adapter.connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            print('Synthetic migrated inventory verified: 5 preserved + 1 new; quick_check OK; file unchanged')
        finally:
            adapter.connection.close()
    else:
        raise ValueError('Expected seed-legacy or verify')


if __name__ == '__main__':
    main()
