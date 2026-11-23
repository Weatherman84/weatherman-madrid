"""Export frozen research numbers; no training, data access or model selection."""

import argparse
import hashlib
import json
from pathlib import Path


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def export(candidate_path, reference_path, output):
    c, r = (json.loads(Path(p).read_text()) for p in (candidate_path, reference_path))
    for artifact in (c, r):
        if artifact["sha256"] != canonical({k: v for k, v in artifact.items() if k != "sha256"}):
            raise ValueError("Research artifact digest mismatch")
    if c["reference_sha256"] != canonical(r["model"]):
        raise ValueError("Frozen candidate/reference mismatch")
    payload = {"candidate_id": c["protocol"]["version"], "airport": "LEMD",
               "checkpoint": c["protocol"]["checkpoint"], "fit_at": c["model"]["fit_at"],
               "research_only": True, "automatic_promotion": False, "automatic_execution": False,
               "source_candidate_sha256": c["sha256"], "source_reference_sha256": r["sha256"],
               "candidate": c["model"], "reference": r["model"],
               "backoff": r["protocol"]["tail"]["shared_EMOS_backoff"],
               "maximum_observation_age_minutes": r["protocol"]["maximum_observation_age_minutes"]}
    body = (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    from weatherman.madrid_late_shadow_model import PARAMETER_SHA
    if hashlib.sha256(body).hexdigest() != PARAMETER_SHA:
        raise ValueError("Export differs from the prespecified parameter freeze")
    output.write_bytes(body)
    output.with_suffix(".sha256").write_text(PARAMETER_SHA + "  " + output.name + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.candidate, args.reference, args.output)
