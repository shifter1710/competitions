import argparse
import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description='Create compressed SQLite backups with retention.')
    parser.add_argument('--db-path', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--keep', type=int, default=14)
    return parser.parse_args()


def verify_archive(archive_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        restored_path = Path(tmp_dir) / 'verify.sqlite3'
        with gzip.open(archive_path, 'rb') as source_file, restored_path.open('wb') as target_file:
            shutil.copyfileobj(source_file, target_file)
        connection = sqlite3.connect(restored_path)
        try:
            result = connection.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            connection.close()
    if result != 'ok':
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(f'backup verification failed: {result}')


def run_backup(db_path: Path, output_dir: Path, keep: int) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)

    timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    temp_path = output_dir / f'competitions-{timestamp}.sqlite3'
    archive_path = output_dir / f'competitions-{timestamp}.sqlite3.gz'

    source = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    destination = sqlite3.connect(temp_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    try:
        with temp_path.open('rb') as source_file, gzip.open(archive_path, 'wb') as gzip_file:
            shutil.copyfileobj(source_file, gzip_file)
        os.chmod(archive_path, 0o600)
        verify_archive(archive_path)
    finally:
        temp_path.unlink(missing_ok=True)

    backups = sorted(output_dir.glob('competitions-*.sqlite3.gz'), reverse=True)
    for backup in backups[keep:]:
        backup.unlink()

    return archive_path


def main():
    args = parse_args()
    archive = run_backup(Path(args.db_path), Path(args.output_dir), args.keep)
    print(archive)


if __name__ == '__main__':
    main()
