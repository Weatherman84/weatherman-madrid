from __future__ import annotations

import argparse
import json

from weatherman.checkpoint_delivery_guard import run_guarded_madrid_collector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("auto", "aviation", "fixed", "closeout"), default="auto")
    args = parser.parse_args()
    result = run_guarded_madrid_collector(requested_mode=args.mode)
    print(json.dumps(result, default=str, sort_keys=True))
    if result.get("status") == "partial":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
