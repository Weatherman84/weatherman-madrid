from __future__ import annotations

import argparse
from pathlib import Path

from weatherman.export_validation import validate_export


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-age-minutes", type=float, default=10)
    args = parser.parse_args()
    payload = validate_export(args.path, max_age_minutes=args.max_age_minutes)
    print(
        {
            "status": "valid",
            "generated_at": payload["generated_at"],
            "last_target_date": payload["window"]["last_target_date"],
        }
    )


if __name__ == "__main__":
    main()
