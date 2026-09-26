from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def last_final_actual_date(payload: dict) -> str:
    dates = sorted(
        str(row.get("target_date") or "")
        for row in payload.get("actuals", [])
        if row.get("is_final_station_actual") is True
    )
    dates = [value for value in dates if len(value) == 10]
    if not dates:
        raise ValueError("Export has no final Stored-METAR actual date")
    return dates[-1]


def publication_metadata(source: Path) -> tuple[dict, bytes]:
    raw = source.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    required = {
        "airport": "LEMD",
        "classification": "READ-ONLY DAILY ANALYSIS EXPORT",
        "contains_credentials": False,
        "writes_production_database": False,
        "research_only": True,
    }
    for field, expected in required.items():
        if payload.get(field) != expected:
            raise ValueError(f"Unsafe publication field {field!r}")
    generated_at = str(payload.get("generated_at") or "")
    if not generated_at:
        raise ValueError("Export has no generated_at")
    target_date = last_final_actual_date(payload)
    return {
        "schema_version": "1.0",
        "status": "prepared",
        "target_date": target_date,
        "generated_at": generated_at,
        "application_version": payload.get("application_version"),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "latest_path": "daily-analysis-latest.json",
        "dated_path": f"daily-analysis/{target_date}.json",
        "research_only": True,
        "writes_production_database": False,
    }, raw


def prepare_publication(source: Path, site_root: Path) -> dict:
    metadata, raw = publication_metadata(source)
    dated = site_root / metadata["dated_path"]
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_bytes(raw)
    (site_root / "daily-analysis-publication.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--site-root", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    metadata = prepare_publication(args.source, args.site_root)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for name in ("target_date", "generated_at", "size_bytes", "sha256"):
                output.write(f"{name}={metadata[name]}\n")
    print(metadata)


if __name__ == "__main__":
    main()
