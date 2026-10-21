"""Synthetic-only B -> A -> B GTO image compatibility inventory."""
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path


ROOT = Path(os.environ.get('GTO_ROUNDTRIP_DATA', '/code/data'))
DATABASE = ROOT / 'competitions.sqlite3'
LEGACY_MARKER = ROOT / 'gto-synthetic-inventory.json'
SNAPSHOT = ROOT / 'gto-roundtrip-inventory.json'
STORED_NAME = re.compile(r'[0-9a-f]{32}\.(?:pdf|png|jpe?g)')


def synthetic_connection():
    if os.environ.get('GTO_SYNTHETIC_IMAGE_SMOKE') != '1' or not LEGACY_MARKER.is_file():
        raise RuntimeError('Roundtrip requires an opted-in synthetic image volume')
    legacy = json.loads(LEGACY_MARKER.read_text())
    if len(legacy.get('old_rows', [])) != 5 or legacy['old_rows'][0][:2] != [20, 1]:
        raise RuntimeError('Unexpected synthetic legacy inventory')
    connection = sqlite3.connect(DATABASE.resolve().as_uri() + '?mode=rw', uri=True)
    connection.row_factory = sqlite3.Row
    student = connection.execute('SELECT full_name FROM students WHERE id=1').fetchone()
    if not student or student['full_name'] != 'Синтетический Студент CI':
        connection.close()
        raise RuntimeError('Unexpected synthetic student')
    return connection


def inventory(connection):
    if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError('SQLite integrity failed')

    def rows(query):
        return [dict(row) for row in connection.execute(query)]

    awards = rows(
        'SELECT id,student_id,year,stage,status,created_at,updated_at,awarded_on '
        'FROM student_gto_records ORDER BY id'
    )
    certificates = rows(
        'SELECT id,gto_record_id,filename,stored_name,content_type,size,uploaded_by,created_at '
        'FROM student_gto_certificates ORDER BY id'
    )
    files = {}
    for certificate in certificates:
        record_id = certificate['gto_record_id']
        name = certificate['stored_name']
        if not isinstance(record_id, int) or record_id <= 0 or not STORED_NAME.fullmatch(name or ''):
            raise RuntimeError('Unsafe certificate reference')
        directory = ROOT / 'files' / 'gto' / str(record_id)
        path = directory / name
        if any(component.is_symlink() for component in (ROOT, ROOT / 'files', ROOT / 'files' / 'gto', directory)):
            raise RuntimeError('Symlinked certificate directory')
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError('Certificate is not a regular file')
        content = path.read_bytes()
        if len(content) != certificate['size']:
            raise RuntimeError('Certificate size mismatch')
        files[path.relative_to(ROOT).as_posix()] = hashlib.sha256(content).hexdigest()
    return {
        'student': dict(connection.execute('SELECT id,full_name,gto_uin FROM students WHERE id=1').fetchone()),
        'awards': awards,
        'certificates': certificates,
        'files': files,
        'sequence': connection.execute("SELECT seq FROM sqlite_sequence WHERE name='student_gto_records'").fetchone()[
            0
        ],
    }


def seed(connection):
    if SNAPSHOT.exists():
        raise RuntimeError('Roundtrip inventory already exists')
    if connection.execute('SELECT COUNT(*) FROM student_gto_certificates').fetchone()[0]:
        raise RuntimeError('Unexpected pre-existing certificates')
    certificate_ids = []
    award_ids = []
    with connection:
        connection.execute("UPDATE students SET gto_uin='000123' WHERE id=1")
        for index, status in enumerate(('bronze', 'silver', 'gold')):
            cursor = connection.execute(
                'INSERT INTO student_gto_records '
                '(student_id,year,stage,status,created_at,updated_at,awarded_on) VALUES (1,2026,8,?,?,?,?)',
                (status, 'synthetic-created', 'synthetic-updated', f'2026-05-0{index + 1}'),
            )
            award_id = cursor.lastrowid
            award_ids.append(award_id)
            name = chr(ord('a') + index) * 32 + '.pdf'
            content = f'%PDF-1.4\nSynthetic certificate {index + 1}\n'.encode()
            directory = ROOT / 'files' / 'gto' / str(award_id)
            directory.mkdir(parents=True, exist_ok=False)
            (directory / name).write_bytes(content)
            cursor = connection.execute(
                'INSERT INTO student_gto_certificates '
                '(gto_record_id,filename,stored_name,content_type,size,uploaded_by,created_at) '
                'VALUES (?,?,?,?,?,NULL,?)',
                (
                    award_id,
                    f'synthetic-certificate-{index + 1}.pdf',
                    name,
                    'application/pdf',
                    len(content),
                    'synthetic-created',
                ),
            )
            certificate_ids.append(cursor.lastrowid)
    result = inventory(connection)
    if result['student']['gto_uin'] != '000123' or len(result['certificates']) != 3:
        raise RuntimeError('Synthetic seed incomplete')
    result['new_award_ids'] = award_ids
    result['new_certificate_ids'] = certificate_ids
    SNAPSHOT.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def verify(connection):
    expected = json.loads(SNAPSHOT.read_text())
    actual = inventory(connection)
    for key in ('student', 'awards', 'certificates', 'files', 'sequence'):
        if actual[key] != expected[key]:
            raise RuntimeError(f'GTO roundtrip mismatch: {key}')
    if len(expected['new_award_ids']) != 3 or len(expected['new_certificate_ids']) != 3:
        raise RuntimeError('Incomplete synthetic inventory')
    return expected


def main():
    if sys.argv[1:] not in (['seed'], ['verify']):
        raise ValueError('Expected seed or verify')
    connection = synthetic_connection()
    try:
        result = seed(connection) if sys.argv[1] == 'seed' else verify(connection)
    finally:
        connection.close()
    digest = hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    print(
        f'GTO roundtrip verified: award IDs {result["new_award_ids"]}, '
        f'certificate IDs {result["new_certificate_ids"]}, inventory SHA256 {digest}'
    )


if __name__ == '__main__':
    main()
