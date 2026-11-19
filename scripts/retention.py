"""Delete raw and failed clips older than config key raw_retention_days.  Run daily:  python -m scripts.retention [--dry-run]"""
import json
import sys

from app import logging_setup, retention


def main(argv: list[str] | None = None) -> dict:
    logging_setup.setup()
    result = retention.run(dry_run="--dry-run" in (argv if argv is not None else sys.argv[1:]))
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    main()
