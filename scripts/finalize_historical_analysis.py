"""Compact the offline summary and package audited research results, without forecasts."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def compact(value):
    if isinstance(value, dict):
        return {
            k: compact(v)
            for k, v in value.items()
            if k not in {"bankroll_curve", "calibration", "stability"}
        }
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def finalize(root):
    summary_path = root / "historical-replay-analysis-summary.json"
    summary = json.loads(summary_path.read_text())
    required = [
        "regime-tournament.json",
        "policy-sensitivity-matrix.json",
        "counterfactual-parameter-study.json",
        "walk-forward-tournament.json",
        "challenger-tournament.json",
        "trading-tournament.json",
        summary_path.name,
    ]
    for name in required:
        payload = json.loads((root / name).read_text())
        assert payload["research_only"] is True and payload["automatic_promotion"] is False
        assert payload["certified_oos"] is False
    assert (
        summary["target_days"],
        summary["checkpoints"],
        summary["accepted_actual_days"],
        summary["evaluable_forecast_checkpoints"],
    ) == (191, 764, 186, 735)
    matrix = json.loads((root / required[1]).read_text())
    assert len(matrix["matrix"]) == 318
    assert len({(e["checkpoint"], e["policy_id"], e["scenario"]) for e in matrix["matrix"]}) == 318
    walk = json.loads((root / required[3]).read_text())
    primary = [w for w in walk["walk_forward"] if w["policy_id"] == "existing_parameter_selector"]
    assert len(primary) == 8
    trades = json.loads((root / "derived/trading-records.json").read_text())["trades"]
    for trade in trades:
        legs = trade["legs"]
        assert abs(sum(leg["stake_weight"] for leg in legs) - 1) < 1e-9
        assert all(leg["age_minutes"] >= 0 and 0 < leg["series_price"] <= 1 for leg in legs)
        if isinstance(trade["freshness_minutes"], int):
            assert all(leg["age_minutes"] <= trade["freshness_minutes"] for leg in legs)
        pnl = sum(
            leg["stake_weight"] * (int(leg["resolved_yes"]) / leg["series_price"] - 1)
            for leg in legs
        )
        assert abs(pnl - trade["pnl"]) < 1e-8
    manifest = json.loads((root / "analysis-input-manifest.json").read_text())
    for path, expected in manifest["inputs"].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
    policy_records = json.loads((root / "derived/analysis-policy-records.json").read_text())[
        "records"
    ]
    assert all(
        r.get("evidence_class") == "historical_partial_fidelity"
        for group in policy_records
        for rows in group["policies"].values()
        for r in rows
    )
    v2 = next(
        g for g in policy_records if g["scenario"] == "main" and g["checkpoint"].startswith("D-1")
    )["policies"]["d1_evening_challenger_v0.2"]
    applied = sum(
        abs(r.get("probability_interaction", {}).get("probability_mass_shift", 0)) > 1e-12
        for r in v2
    )
    summary["d1_v2_boundary_taf_nonzero_mass_shift_main_cases"] = applied
    summary["walk_forward_monthly_and_half_stability"] = [
        {
            "checkpoint": r["checkpoint"],
            "window": r["window"],
            "partitions": {
                k: {
                    field: v.get(field)
                    for field in ["matched_n", "accuracy_delta_pp", "mae_delta_c"]
                }
                for k, v in r["stability"].items()
                if k.startswith("month:")
                or k
                in {
                    "first_half",
                    "second_half",
                    "existing_persistent_hot",
                    "without_persistent_hot",
                }
            },
        }
        for r in primary
    ]
    summary["no_trade_60m"] = [
        r for r in summary["no_trade_60m"] if r["strategy"] == "A_champion_top1"
    ]
    summary["no_trade_summary_scope"] = "Champion Top1; all strategies in no-trade-research.json"
    summary["challenger_rankings"] = {
        cp: [r for r in rows if r["matched_n"] == summary["baseline"][cp]["n"]][:5]
        for cp, rows in summary["challenger_rankings"].items()
    }
    summary["summary_scope"] = (
        "Compact key results; full calibration bins, curves, conditional ablations and regime/month partitions in master JSON files."
    )
    summary_path.write_text(json.dumps(compact(summary), ensure_ascii=False, indent=2) + "\n")
    # Editorial conclusions derived from the complete master outputs, not a new model or rule.
    report = root / "historical-replay-analysis-report.md"
    content = report.read_text()
    marker = "## Einordnung der vollständigen Ergebnisse"
    if marker not in content:
        content += f"""\n{marker}\n
D0 Anchor 25% ist der deutlichste Parameter-Kandidat für weiteres Shadow-Research:
51,37% In-Sample Accuracy gegenüber 47,54% Champion, positive Accuracy- und
MAE-Deltas in allen sechs Sensitivitäten. Der Selector gewinnt expanding nur
0,55 pp, rolling 90 Tage 2,19 pp. Die In-Sample-Verbesserung verteilt sich
ungleich: April −3,57 pp, Juni +14,29 pp, September +10,34 pp.

Late Dry Mixing 0% erreicht 61,96% statt 59,24%; der Walk-Forward-Selector
erreicht 61,41% (+2,17 pp). Seine Treffergewinne liegen im August/September;
erste Hälfte 0 pp, zweite Hälfte +4,26 pp. Nur fünf Archivannahmen sind
evaluierbar: complete_single_run_only liefert bei First/Late Live keinen Forecast.
Kein Nachweis stabiler Ganzjahresleistung.

D−1: 25% v0.2-Bias bringt nachträglich einen zusätzlichen Treffer, aber leicht
schlechtere MAE; beide Walk-Forward-Selectoren gewinnen keine Treffer. Die
unveränderten v0.1/v0.2-Challenger verschlechtern sich über alle sechs
Sensitivitäten. First Live bleibt beim geschützten Champion. Die entdeckte
v0.2-TAF-Codeauffälligkeit führte im Hauptlauf zu **{applied} Fällen mit einem
nichtnull Wahrscheinlichkeits-Shift**; sie verändert dort also keine
Treffer, muss vor einer künftigen Zertifizierung separat geprüft werden.

Robuste positive Regime-Assoziationen: Stable/High Confidence, insbesondere
D−1 (+10,39 pp, N79) und D0 (+13,75 pp, N62). D0 Temperature Plateau ist
deutlich schlechter (−19,34 pp, N39); Negative Anchor und Bucket Boundary
sind ebenfalls konsistent negativ. Live-Regimes erlauben wegen der fehlenden
Single-Run-Sensitivität keine vollständige Sechs-Szenarien-Robustheitsbehauptung.

Trading: Die einfachen Top1-Baskets sind bei breiter Stichprobe vorteilhafter
als positive-EV-only. Letzteres verliert D0/First/Late mit ROI −22,54% /
−18,77% / −17,74%; größter Drawdown 84,05 Einheiten bei Late Live.
Höherer berechneter Edge bedeutet nicht monoton bessere Ergebnisse. First
Live >20 pp Edge: nur 7,55% tatsächliche Treffer gegen 37,47% Prognose und
12,14% Marktpreis; kein überzeugender allgemeiner Edge-Cutoff.

Ein Ausschluss von Top1/Top2-Abständen unter 15 pp erhöht die hypothetische
Top1-ROI bei D−1 von 18,78% auf 33,04% und D0 von 15,37% auf 37,20%,
bei weniger Trades und geringerem Drawdown. Bei First/Late erhöht derselbe
Filter die ROI nicht. Die sehr hohen G-Filter-ROIs beruhen auf nur 7–22 Fällen
und unvollständigen Production-Gates; daraus folgt keine robuste Handelsfreigabe.

Diese Befunde begründen weitere echte Shadow-Beobachtung, keine Parameter-
oder Trading-Promotion. Es wurden keine neuen Datenquellen oder Regimes benutzt.
"""
        report.write_text(content)
    validation = {
        "research_only": True,
        "automatic_promotion": False,
        "certified_oos": False,
        "master_files": len(required),
        "policy_sensitivity_cells": 318,
        "walk_forward_entries": len(walk["walk_forward"]),
        "price_trade_scenario_records": len(trades),
        "source_immutability_verified": True,
        "evidence_class_preserved": True,
        "all_trade_legs_causal_and_within_freshness": True,
        "payout_and_stake_accounting_verified": True,
        "complete_source_actual_counts_verified": True,
        "d1_v2_boundary_taf_nonzero_mass_shift_main_cases": applied,
        "finalizer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "master_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in required
        },
    }
    (root / "final-validation.json").write_text(json.dumps(validation, indent=2) + "\n")
    zip_path = root.parent / "Madrid-Historical-Replay-Analysis-v1.zip"
    files = sorted(root.glob("*.json")) + sorted(root.glob("*.md")) + sorted(root.glob("*.txt"))
    files += sorted((root / "inputs").glob("*.json"))
    files += [
        root / "derived" / name
        for name in [
            "cross-check-records.json",
            "analysis-policy-records.json",
            "trading-records.json",
        ]
    ]
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for file in files:
            archive.write(
                file, str(Path("Madrid-Historical-Replay-Analysis-v1") / file.relative_to(root))
            )
    print(
        json.dumps(
            {
                "summary_bytes": summary_path.stat().st_size,
                "zip_bytes": zip_path.stat().st_size,
                "zip_files": len(files),
                "validation": validation,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    finalize(parser.parse_args().output.resolve())
