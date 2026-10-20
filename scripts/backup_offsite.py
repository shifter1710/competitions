"""Upload fresh verified sets to the existing rclone crypt remote, without cleanup."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.backup_offsite import upload_fresh  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', default=[])
    parser.add_argument('--set-dir', help='upload an existing verified release set without making another backup')
    parser.add_argument('--remote', required=True, help='existing crypt remote path; never credentials')
    parser.add_argument('--config')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--max-age-hours', type=float, default=26)
    args = parser.parse_args()
    try:
        upload_fresh(
            args.root,
            args.remote,
            args.state_dir,
            config=args.config,
            max_age=args.max_age_hours * 3600,
            set_dir=args.set_dir,
        )
    except Exception:
        print('Offsite verification failed; see private operational status.', file=sys.stderr)
        return 1
    print('Fresh offsite set verified.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
