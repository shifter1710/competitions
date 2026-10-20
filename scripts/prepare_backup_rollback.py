"""Freeze a private, verified NO-DELETE operations bundle and app-only rollback overlay."""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.backup import sha256  # noqa: E402
from src.ops_state import atomic_json  # noqa: E402

CODE_FILES = [
    'src/__init__.py',
    'src/backup.py',
    'src/ops_state.py',
    'src/backup_offsite.py',
    'src/health.py',
    'src/telegram_alerts.py',
    'scripts/backup_sqlite.py',
    'scripts/backup_offsite.py',
    'scripts/health_check.py',
    'scripts/restore_backup.py',
    'scripts/restore_drill.py',
    'scripts/prepare_backup_rollback.py',
]
UNITS = [
    'competitions-backup.service',
    'competitions-backup.timer',
    'competitions-backup-offsite.service',
    'competitions-backup-offsite.timer',
    'competitions-health.service',
    'competitions-health.timer',
]


def verify_bundle(destination):
    destination = Path(destination)
    manifest = json.loads((destination / 'bundle.json').read_text())
    if manifest.get('schema') != 1 or set(manifest.get('files', {})) != set(
        CODE_FILES + ['systemd/' + name for name in UNITS] + ['rollback.override.json']
    ):
        raise ValueError('invalid rollback bundle')
    for name, digest in manifest['files'].items():
        path = destination / name
        if path.is_symlink() or sha256(path) != digest:
            raise ValueError('rollback bundle integrity failed')
    return manifest


def prepare_bundle(destination, revision, previous_image, app_root):
    source = Path(__file__).resolve().parent.parent
    destination = Path(destination).resolve()
    if source == destination or source in destination.parents:
        raise ValueError('rollback bundle must be outside the repository')
    if not re.fullmatch(r'[0-9a-f]{7,40}', revision):
        raise ValueError('an exact checked revision is required')
    if not previous_image or any(character.isspace() for character in previous_image):
        raise ValueError('a preserved local image reference is required')
    app_root = str(Path(app_root).resolve())
    if any(character.isspace() for character in str(destination) + app_root):
        raise ValueError('systemd bundle paths must not contain whitespace')
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name in CODE_FILES:
        target = destination / name
        target.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(source / name, target)
        target.chmod(0o600)
    unit_dir = destination / 'systemd'
    unit_dir.mkdir(mode=0o700)
    for name in UNITS:
        content = (source / 'deploy' / 'systemd' / name).read_text()
        content = content.replace('/root/chatgpt/competitions', app_root)
        content = content.replace(app_root + '/scripts/', str(destination) + '/scripts/')
        content = content.replace('WorkingDirectory=' + app_root, 'WorkingDirectory=' + str(destination))
        target = unit_dir / name
        target.write_text(content)
        target.chmod(0o600)
    atomic_json(
        destination / 'rollback.override.json',
        {
            'services': {
                'competitions': {
                    'image': previous_image,
                    'volumes': [
                        {
                            'type': 'bind',
                            'source': str(destination / 'src' / name),
                            'target': '/code/src/' + name,
                            'read_only': True,
                        }
                        for name in ('backup.py', 'ops_state.py')
                    ],
                }
            }
        },
    )
    names = CODE_FILES + ['systemd/' + name for name in UNITS] + ['rollback.override.json']
    atomic_json(
        destination / 'bundle.json',
        {'schema': 1, 'revision': revision, 'files': {name: sha256(destination / name) for name in names}},
    )
    verify_bundle(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--revision')
    parser.add_argument('--previous-image')
    parser.add_argument('--app-root', default='/root/chatgpt/competitions')
    args = parser.parse_args()
    try:
        if args.verify:
            verify_bundle(args.destination)
        else:
            prepare_bundle(args.destination, args.revision or '', args.previous_image, args.app_root)
    except Exception:
        print('NO-DELETE rollback bundle verification failed.', file=sys.stderr)
        return 1
    print('NO-DELETE rollback bundle verified; no services were changed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
