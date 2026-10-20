"""Validate and extract a trusted backup set into a NEW isolated directory only.

This is archive verification, NOT a full application restore test and never records PASS.
Use the documented isolated app smoke before recording a restore result.
"""
import argparse
import gzip
import os
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.backup import reference_names, sha256, verify_set  # noqa: E402
from src.ops_state import atomic_json  # noqa: E402
from src.backup_offsite import rclone  # noqa: E402


def extract_set(source, destination):
    manifest = verify_set(source)
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        with gzip.open(Path(source) / 'database.sqlite3.gz', 'rb') as stream, (
            destination / 'competitions.sqlite3'
        ).open('wb') as target:
            shutil.copyfileobj(stream, target)
        os.chmod(destination / 'competitions.sqlite3', 0o600)
        files = destination / 'files'
        files.mkdir(mode=0o700)
        with zipfile.ZipFile(Path(source) / 'files.zip') as archive:
            for item in archive.infolist():
                target = files / item.filename
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.open(item) as stream, target.open('xb') as output:
                        shutil.copyfileobj(stream, output)
                    os.chmod(target, 0o600)
            if not all((files / name).is_file() for name in reference_names(destination / 'competitions.sqlite3')):
                raise ValueError('reference verification failed')
        return manifest
    except Exception:
        # Preserve a failed drill for private inspection; never touch any live directory.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--set-dir', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--remote-set', help='optional exact set path on existing crypt remote')
    parser.add_argument('--config')
    parser.add_argument('--trusted-manifest-sha256', help='required for a remotely downloaded set')
    args = parser.parse_args()
    try:
        if args.remote_set:
            if not args.trusted_manifest_sha256:
                parser.error('remote download requires independently preserved manifest SHA256')
            Path(args.set_dir).mkdir(mode=0o700, exist_ok=False)
            rclone(['copy', args.remote_set, args.set_dir, '--immutable'], args.config)
            if sha256(Path(args.set_dir) / 'manifest.json') != args.trusted_manifest_sha256:
                raise ValueError('untrusted manifest')
            atomic_json(Path(args.set_dir) / '.remote-verified.json', {'manifest_sha256': args.trusted_manifest_sha256})
        extract_set(args.set_dir, args.destination)
    except Exception:
        print('Isolated archive verification failed.', file=sys.stderr)
        return 1
    print('Archives and references verified in isolated directory. Full application restore is NOT yet verified.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
