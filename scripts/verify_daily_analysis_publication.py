from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _validate_bytes(raw: bytes, *, sha256: str, size: int, target_date: str,
                    generated_at: str) -> None:
    if len(raw) != size:
        raise ValueError(f"size mismatch: {len(raw)} != {size}")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != sha256:
        raise ValueError(f"SHA-256 mismatch: {digest} != {sha256}")
    payload = json.loads(raw.decode("utf-8"))
    expected = {
        "airport": "LEMD",
        "classification": "READ-ONLY DAILY ANALYSIS EXPORT",
        "contains_credentials": False,
        "writes_production_database": False,
        "research_only": True,
        "generated_at": generated_at,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ValueError(f"unexpected {field}: {payload.get(field)!r}")
    actual_dates = {
        str(row.get("target_date"))
        for row in payload.get("actuals", [])
        if row.get("is_final_station_actual") is True
    }
    if target_date not in actual_dates:
        raise ValueError(f"final actual date {target_date} is missing")


def _verify_endpoint(base: str, *, sha256: str, size: int, target_date: str,
                     generated_at: str, attempts: int, wait_seconds: float) -> dict:
    latest_url = _url(base, f"daily-analysis-latest.json?verify={sha256[:12]}")
    dated_url = _url(base, f"daily-analysis/{target_date}.json")
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(Request(latest_url, headers={"User-Agent": "Weatherman-Publish-Check/1.0"}),
                         timeout=30) as response:
                raw = response.read()
            _validate_bytes(
                raw, sha256=sha256, size=size, target_date=target_date,
                generated_at=generated_at,
            )
            with urlopen(Request(dated_url, method="HEAD"), timeout=30) as response:
                if response.status != 200:
                    raise ValueError(f"dated endpoint returned HTTP {response.status}")
            return {
                "status": "verified",
                "latest_url": latest_url.split("?", 1)[0],
                "dated_url": dated_url,
                "attempts": attempt,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            last_error = f"{type(error).__name__}: {error}"
            if attempt < attempts:
                time.sleep(wait_seconds)
    return {
        "status": "failed",
        "latest_url": latest_url.split("?", 1)[0],
        "dated_url": dated_url,
        "attempts": attempts,
        "error": last_error,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-base", required=True)
    parser.add_argument("--mirror-base", default="")
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size-bytes", type=int, required=True)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--wait-seconds", type=float, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = {
        "schema_version": "1.0",
        "target_date": args.target_date,
        "generated_at": args.generated_at,
        "size_bytes": args.size_bytes,
        "sha256": args.sha256,
        "endpoints": {
            "github_pages": _verify_endpoint(
                args.primary_base, sha256=args.sha256, size=args.size_bytes,
                target_date=args.target_date, generated_at=args.generated_at,
                attempts=args.attempts, wait_seconds=args.wait_seconds,
            ),
            "cloudflare_kv_mirror": (
                _verify_endpoint(
                    args.mirror_base, sha256=args.sha256, size=args.size_bytes,
                    target_date=args.target_date, generated_at=args.generated_at,
                    attempts=args.attempts, wait_seconds=args.wait_seconds,
                )
                if args.mirror_base
                else {"status": "not_configured"}
            ),
        },
        "production_database_queries": 0,
    }
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    failed = [
        name for name, endpoint in report["endpoints"].items()
        if endpoint["status"] == "failed"
    ]
    if failed:
        raise SystemExit(f"Publication verification failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
