"""Executable synthetic checks for the no-delete app rollback bridge and CLI fallback."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from scripts.prepare_backup_rollback import prepare_bundle
from scripts.prepare_backup_rollback import verify_bundle


@pytest.fixture
def bundle(tmp_path):
    return prepare_bundle(tmp_path / 'pinned-ops', '0123456789abcdef', 'example-app:preserved', tmp_path / 'app')


def test_bundle_pins_units_and_minimal_read_only_app_bridge(bundle):
    manifest = verify_bundle(bundle)
    assert manifest['revision'] == '0123456789abcdef'
    service = (bundle / 'systemd' / 'competitions-backup.service').read_text()
    assert f'WorkingDirectory={bundle}' in service
    assert str(bundle / 'scripts' / 'backup_sqlite.py') in service
    assert '--db-path' in service and '--files-dir' in service
    overlay = json.loads((bundle / 'rollback.override.json').read_text())['services']['competitions']
    assert overlay['image'] == 'example-app:preserved'
    assert {item['target'] for item in overlay['volumes']} == {'/code/src/backup.py', '/code/src/ops_state.py'}
    assert all(item['read_only'] for item in overlay['volumes'])
    assert all(item['source'].startswith(str(bundle)) for item in overlay['volumes'])
    assert (bundle / 'src' / '__init__.py').exists()
    assert not (bundle / '.env').exists()
    (bundle / 'src' / 'backup.py').write_text('synthetic accidental change')
    with pytest.raises(ValueError, match='integrity'):
        verify_bundle(bundle)


def test_legacy_positional_caller_with_two_module_overlay_never_deletes_old_backups(bundle, tmp_path):
    old_image = tmp_path / 'old-image'
    (old_image / 'src').mkdir(parents=True)
    (old_image / 'src' / '__init__.py').touch()
    for name in ('backup.py', 'ops_state.py'):
        shutil.copyfile(bundle / 'src' / name, old_image / 'src' / name)
    # Only these two modules are exposed, as with the documented Docker bind overlay.
    script = '''
import sqlite3, sys
from pathlib import Path
from src.backup import run_backup
root=Path(sys.argv[1]); root.mkdir()
db=root/'synthetic.sqlite3'
with sqlite3.connect(db) as conn:
    conn.execute('CREATE TABLE synthetic (id INTEGER)')
files=root/'files'; files.mkdir()
output=root/'backups'; output.mkdir()
old={}
for number in range(3):
    name=f'competitions-2000010{number+1}-000000.sqlite3.gz'
    old[name]=('synthetic-prior-'+str(number)).encode()
    (output/name).write_bytes(old[name])
# Old application calls this exact positional API, including keep=1.
result=run_backup(db, output, 1, files)
assert result['archive'].is_file() and result['files_archive'].is_file()
assert result['timestamp'] and result['manifest'].is_file()
assert all((output/name).read_bytes()==content for name,content in old.items())
assert 'src.health' not in sys.modules and 'src.telegram_alerts' not in sys.modules
'''
    environment = dict(os.environ, PYTHONPATH=str(old_image))
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path / 'synthetic-runtime')],
        cwd=old_image,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, 'Synthetic legacy bridge failed'


def test_pinned_cli_fallback_creates_verified_set_and_offsite_uses_same_namespace(bundle, tmp_path):
    data = tmp_path / 'synthetic-data'
    data.mkdir()
    db = data / 'synthetic.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE synthetic (id INTEGER)')
    files = data / 'files'
    files.mkdir()
    backups = data / 'backups'
    backups.mkdir()
    old = backups / 'competitions-20000101-000000.sqlite3.gz'
    old.write_bytes(b'synthetic-old-backup')
    result = subprocess.run(
        [
            sys.executable,
            str(bundle / 'scripts' / 'backup_sqlite.py'),
            '--db-path',
            str(db),
            '--files-dir',
            str(files),
            '--output-dir',
            str(backups),
            '--keep',
            '1',
            '--source',
            'daily',
            '--state-dir',
            str(data / 'ops'),
            '--no-audit',
        ],
        cwd=bundle,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, 'Synthetic pinned CLI failed'
    assert old.read_bytes() == b'synthetic-old-backup'
    sets = list(backups.glob('sets/*'))
    assert len(sets) == 1
    script = '''
import sys
from pathlib import Path
from src.backup import verify_set
from src.backup_offsite import upload_fresh
from src import backup_offsite
root=Path(sys.argv[1]); state=Path(sys.argv[2]); commands=[]
def rclone(args, config=None):
    commands.append(args)
    if args[0]=='cryptcheck':
        names=sorted(path.name for path in Path(args[1]).iterdir() if path.is_file())
        Path(args[args.index('--combined')+1]).write_text(''.join('= '+name+'\\n' for name in names))
        return f'0 differences found\\n{len(names)} matching files'.encode()
    return b''
backup_offsite.rclone=rclone
manifest=verify_set(next(root.glob('sets/*')))
result=upload_fresh([root], 'synthetic-crypt:backups', state)
assert result['id']==manifest['id']
assert all('/sets/'+manifest['id'] in args[2] for args in commands)
assert len(list(root.glob('sets/*')))==1
'''
    result = subprocess.run(
        [sys.executable, '-c', script, str(backups), str(data / 'ops')],
        cwd=bundle,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, 'Synthetic pinned offsite namespace compatibility failed'
    assert old.read_bytes() == b'synthetic-old-backup'
