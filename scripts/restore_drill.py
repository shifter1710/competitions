"""Full isolated Docker restore drill. Never mounts or replaces live application data."""
import argparse
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.restore_backup import extract_set  # noqa: E402
from src.backup import reference_names, sha256  # noqa: E402
from src.ops_state import read_json, update_component, utcnow  # noqa: E402


def command(arguments, *, timeout=30):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError('isolated_drill_step_failed')
    return result.stdout


def drill(set_dir, image, state_dir, revision, *, production=False):  # noqa: C901
    # Explicit isolated steps and cleanup remain together for operational review.
    set_dir = Path(set_dir)
    if production:
        marker = read_json(set_dir / '.remote-verified.json')
        if marker.get('manifest_sha256') != sha256(set_dir / 'manifest.json'):
            raise ValueError('production drill requires independently authenticated offsite download')
    container = 'competitions-restore-drill-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='competitions-restore-drill-') as temporary:
        restored = Path(temporary) / 'data'
        try:
            manifest = extract_set(set_dir, restored)
            # Pass generated drill credentials via environment, never argv or output.
            environment = os.environ.copy()
            environment.update(
                AUTH_SECRET_KEY=secrets.token_hex(32),
                AUTH_ADMIN_PASSWORD=secrets.token_urlsafe(32),
                AUTH_EDITOR_PASSWORD=secrets.token_urlsafe(32),
            )
            args = [
                'docker',
                'run',
                '--detach',
                '--name',
                container,
                '--network',
                'none',
                '--read-only',
                '--user',
                f'{os.getuid()}:{os.getgid()}',
                '--cap-drop',
                'ALL',
                '--security-opt',
                'no-new-privileges',
                '--memory',
                '384m',
                '--cpus',
                '1',
                '--tmpfs',
                '/tmp:rw,nosuid,noexec,size=64m',
                '-v',
                str(restored) + ':/code/data',
                '-e',
                'DATA_FOLDER=/code/data',
                '-e',
                'DATABASE_PATH=/code/data/competitions.sqlite3',
                '-e',
                'AUTH_SECRET_KEY',
                '-e',
                'AUTH_ADMIN_PASSWORD',
                '-e',
                'AUTH_EDITOR_PASSWORD',
                image,
            ]
            # ENV is supplied only to this subprocess; no shell, no inherited live .env mount.
            result = subprocess.run(args, env=environment, capture_output=True, timeout=60, check=False)
            if result.returncode:
                raise RuntimeError('isolated_start_failed')
            probe = (
                'import urllib.request; '
                "r=urllib.request.urlopen('http://127.0.0.1:8080/healthcheck',timeout=3); "
                "assert r.status==200 and r.read().strip()==b'OK'; "
                "r=urllib.request.urlopen('http://127.0.0.1:8080/login',timeout=3); assert r.status==200"
            )
            for attempt in range(30):
                try:
                    command(['docker', 'exec', container, 'python3', '-c', probe], timeout=10)
                    break
                except Exception:
                    if attempt == 29:
                        raise RuntimeError('isolated_health_failed')
                    time.sleep(1)
            if not all(
                (restored / 'files' / name).is_file() for name in reference_names(restored / 'competitions.sqlite3')
            ):
                raise RuntimeError('isolated_references_failed')
            command(['docker', 'rm', '--force', container])
            # Synthetic evidence remains explicitly synthetic and cannot overwrite a real PASS.
            component = 'restore' if production else 'restore_synthetic'
            update_component(
                state_dir,
                component,
                {
                    'status': 'OK',
                    'code': 'pass' if production else 'synthetic_pass',
                    'tested_at': utcnow(),
                    'set_id': manifest['id'],
                    'revision': revision,
                    'steps': ['archives', 'references', 'isolated_start', 'http_smoke'],
                    'scope': 'offsite' if production else 'synthetic',
                },
            )
            return manifest
        except Exception:
            update_component(
                state_dir,
                'restore' if production else 'restore_synthetic',
                {'status': 'CRITICAL', 'code': 'restore_failed'},
            )
            raise
        finally:
            # Only the randomly named container and this TemporaryDirectory are removed.
            subprocess.run(['docker', 'rm', '--force', container], capture_output=True, timeout=30, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--set-dir', required=True)
    parser.add_argument('--image', required=True, help='known tested application image; no live data mounts')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--production-drill', action='store_true')
    args = parser.parse_args()
    try:
        drill(args.set_dir, args.image, args.state_dir, args.revision, production=args.production_drill)
    except Exception:
        print('Isolated full restore drill FAILED.', file=sys.stderr)
        return 1
    print('Isolated full restore drill PASS (' + ('offsite' if args.production_drill else 'synthetic') + ').')
    return 0


if __name__ == '__main__':
    sys.exit(main())
