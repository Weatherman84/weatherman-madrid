"""Compare offline replay scores, allowing tiny BLAS/platform floating-point variation."""
import argparse
import json
import math
from pathlib import Path


def verify(actual, expected):
    if actual["status"] != "NO RELEASE CANDIDATE" or actual["status"] != expected["status"]:
        raise ValueError("Unexpected release status")
    if "frozen_selection" in expected and actual["frozen_selection"] != expected["frozen_selection"]:
        raise ValueError("Frozen selection changed")
    def key(r):
        return r["phase"], r["checkpoint"], r.get("evidence_class", ""), r["candidate"]
    want = {key(r): r for r in expected["summaries"]}
    got = {key(r): r for r in actual["summaries"]}
    if got.keys() != want.keys():
        raise ValueError("Cohort/candidate membership changed")
    for k, row in want.items():
        for metric in ("n", "brier", "log_loss", "mae", "top1", "top2", "coverage80", "width80"):
            if not math.isclose(got[k][metric], row[metric], rel_tol=1e-6, abs_tol=1e-7):
                raise ValueError(f"Score changed: {k} {metric}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actual", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    args = parser.parse_args()
    verify(json.loads(args.actual.read_text()), json.loads(args.expected.read_text()))
    print("PASS: offline cohort membership, frozen selection and scores reproduced")
