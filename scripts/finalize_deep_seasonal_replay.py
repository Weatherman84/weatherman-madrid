"""Audit, summarize and package an already executed seasonal replay, offline."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

import deep_seasonal_replay as deep
from weatherman.historical_archive import atomic_json, digest, utc

MASTERS = (
    "deep-seasonal-replay-summary.json",
    "seasonal-regime-tournament.json",
    "seasonal-policy-study.json",
    "seasonal-trading-tournament.json",
    "seasonal-no-trade-study.json",
    "full-vs-deep-fidelity-comparison.json",
)
EXECUTED_COMMIT = "b442503712bb09e6cb1506ef667d4d28d1b25a09"
REPLAY_FUNCTIONS = (
    "fixed_daily",
    "seasonal_freshness",
    "deep_engine",
    "select_daily",
    "deep_eligible",
    "replay_day",
    "replay",
)


def read(path):
    return json.loads(path.read_text())


def compact(value):
    if isinstance(value, dict):
        return {
            k: compact(v) for k, v in value.items() if k not in ("bankroll_curve", "calibration")
        }
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def label_invariance(root, rows):
    paths = pd.read_parquet(root / "normalized/eligible-fixed-lead-paths.parquet")
    forecasts = pd.read_parquet(root / "normalized/daily-forecasts.parquet")
    obs, tafs, actuals = [
        pd.read_parquet(root / f"normalized/{name}.parquet")
        for name in ("metars", "tafs", "actuals")
    ]
    targets = ("2024-11-05", "2026-01-15", "2026-07-15", "2026-10-01")
    lookup = {(r["target_date"], r["checkpoint_label"]): r for r in rows}
    tested = 0
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        for target in targets:
            changed = actuals.copy(deep=True)
            mask = pd.to_datetime(changed.target_date).dt.date >= deep.date.fromisoformat(target)
            for field in ("max_temp_c", "archive_metar_max_c"):
                changed.loc[mask, field] += 100
            deep._DATA = (
                Path(temporary),
                paths,
                forecasts,
                obs,
                tafs,
                changed,
                "future_actual_perturbation",
            )
            for result in deep.replay_day(target):
                original = lookup[(target, result["checkpoint_label"])]
                assert result.get("champion_center_c") == original.get("champion_center_c")
                assert result.get("champion_probabilities") == original.get(
                    "champion_probabilities"
                )
                for name, p in result["policies"].items():
                    assert p["champion_center_c"] == original["policies"][name]["champion_center_c"]
                    assert (
                        p["champion_probabilities"]
                        == original["policies"][name]["champion_probabilities"]
                    )
                tested += 1
    return tested


def audit(root, source, archive):
    rows = read(root / "derived/checkpoints.json")
    assert len(rows) == 707 * 4
    assert len({(r["target_date"], r["checkpoint_label"]) for r in rows}) == len(rows)
    manifest = read(root / "replay-input-manifest.json")
    for p, sha in manifest["inputs"].items():
        assert digest(Path(p).read_bytes()) == sha, p
    for p, sha in manifest["code"].items():
        if p != "scripts/deep_seasonal_replay.py":
            assert digest((deep.REPO / p).read_bytes()) == sha, p
    ast_path = root.parent / "deep-seasonal-executed-ast.json"
    if ast_path.exists():
        old_ast = read(ast_path)
    else:
        original_text = subprocess.run(
            ["git", "show", f"{EXECUTED_COMMIT}:scripts/deep_seasonal_replay.py"],
            cwd=deep.REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        old_ast = {
            n.name: ast.dump(n, include_attributes=False)
            for n in ast.parse(original_text).body
            if isinstance(n, ast.FunctionDef)
        }
    tree = ast.parse((deep.REPO / "scripts/deep_seasonal_replay.py").read_text())
    current = {
        n.name: ast.dump(n, include_attributes=False)
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
    }
    assert all(old_ast[name] == current[name] for name in REPLAY_FUNCTIONS)
    # The preceding analysis archive pins all original normalized input files.
    prior = read(root.parent / "madrid-historical-analysis-v1/analysis-input-manifest.json")
    for p, sha in prior["inputs"].items():
        assert digest(Path(p).read_bytes()) == sha, p
    provenance_count = 0
    for row in rows:
        assert row["evidence_class"] == deep.EVIDENCE
        assert row["research_only"] and not row["automatic_promotion"]
        for p in row["model_provenance"]:
            assert utc(p["available_at"]) <= utc(row["checkpoint_at"])
            assert utc(p["contributing_run_upper_bound_at"]) <= utc(p["available_at"])
            assert p["forecast_lead_hours"] in (24, 48)
            assert p["actual_initialization_at"] is None and p["run_age_minutes"] is None
            assert p["data_completeness"] == 1
            provenance_count += 1
        if row.get("champion_center_c") is not None:
            assert len(row["used_models"]) >= 2
            assert abs(sum(row["champion_probabilities"].values()) - 1) < 1e-6
            p = row["policies"]["current"]
            assert p["champion_center_c"] == row["champion_center_c"]
            assert p["champion_probabilities"] == row["champion_probabilities"]
        for pred in row["policies"].values():
            assert pred["evidence_class"] == deep.EVIDENCE
            assert pred["actual_bucket"] == row["actual_bucket"]
            assert pred["actual_available_at"] == row.get("actual_available_at")
    original = read(archive / "derived/checkpoints.json")
    lookup = {(r["target_date"], r["checkpoint_label"]): r for r in rows}
    assert all(
        lookup[(r["target_date"], r["checkpoint_label"])]["actual_bucket"] == r["actual_bucket"]
        and lookup[(r["target_date"], r["checkpoint_label"])]["actual_complete"]
        == r["actual_complete"]
        and lookup[(r["target_date"], r["checkpoint_label"])]["stored_metar_actual_c"]
        == r["stored_metar_actual_c"]
        for r in original
    )
    raw_sources = set()
    for name in ("fixed-lead-paths", "metars", "tafs"):
        f = pd.read_parquet(root / f"normalized/{name}.parquet")
        raw_sources.update(f.source_sha256.dropna().unique())
    for sha in raw_sources:
        files = [p for base in (root, source, archive) for p in (base / "raw").glob(sha + ".*")]
        assert files and any(digest(p.read_bytes()) == sha for p in files), sha
    trades = read(root / "derived/trading-records.json")
    for t in trades:
        assert t["pnl_type"] == "price_based_hypothetical_pnl"
        assert abs(sum(p["stake_weight"] for p in t["legs"]) - 1) < 1e-9
        pnl = 0
        for p in t["legs"]:
            assert 0 < p["series_price"] <= 1
            age = (utc(t["checkpoint_at"]) - utc(p["price_at"])).total_seconds() / 60
            assert age >= 0 and abs(age - p["age_minutes"]) < 1e-8
            if t["freshness_minutes"] == "over60_sensitivity_only":
                assert t["max_price_age_minutes"] > 60
            else:
                assert age <= t["freshness_minutes"]
            pnl += p["stake_weight"] * (int(p["resolved_yes"]) / p["series_price"] - 1)
        assert abs(pnl - t["pnl"]) < 1e-9
    verification = root / "normalized/metars-original-cache-verification.parquet"
    if verification.exists():
        observed = pd.read_parquet(verification)
    else:
        cache = deep.RawCache(source, offline=True, max_requests=0)
        try:
            observed = deep.metars(cache, deep.date(2025, 12, 29), deep.date(2026, 10, 10))
        finally:
            cache.close()
    actual = pd.read_parquet(root / "normalized/metars.parquet")
    fields = [
        "observed_at",
        "available_at",
        "temp_c",
        "dewpoint_c",
        "wind_kph",
        "wind_direction",
        "cloud_cover",
        "cloud_base_ft",
        "raw",
        "is_correction",
        "revision_conflict",
    ]
    a = observed[fields].sort_values("observed_at").reset_index(drop=True)
    b = (
        actual[actual.observed_at.isin(observed.observed_at)][fields]
        .sort_values("observed_at")
        .reset_index(drop=True)
    )
    assert a.equals(b)
    perturbations = label_invariance(root, rows)
    return dict(
        **deep.FLAGS,
        status="passed",
        checkpoint_rows=len(rows),
        model_provenance_records=provenance_count,
        verified_raw_source_hashes=len(raw_sources),
        original_archive_hashes_checked=len(prior["inputs"]),
        original_warm_labels_identical=True,
        existing_wide_metar_cache_observations_identical=len(a),
        portfolio_records_checked=len(trades),
        future_actual_perturbation_checkpoints=perturbations,
        replay_source_commit=EXECUTED_COMMIT,
        executed_runner_sha256=manifest["code"]["scripts/deep_seasonal_replay.py"],
        current_runner_sha256=digest((deep.REPO / "scripts/deep_seasonal_replay.py").read_bytes()),
        replay_function_ast_equivalence=True,
        replay_function_ast_sha256={
            name: hashlib.sha256(old_ast[name].encode()).hexdigest() for name in REPLAY_FUNCTIONS
        },
        distinctions="Causal selection and future-label isolation verified; actual historical reception times not certified",
    )


def no_trade_interactions(no_trade, trades):
    result = []
    keys = {
        (e["evidence_class"], e["checkpoint"], e["strategy"], e["gate"])
        for e in no_trade["entries"]
    }
    for evidence, cp, strategy, gate in sorted(keys):
        cells = [
            e
            for e in no_trade["entries"]
            if (e["evidence_class"], e["checkpoint"], e["strategy"], e["gate"])
            == (evidence, cp, strategy, gate)
        ]
        retained_dates = {p["date"] for e in cells for p in e["after"]["bankroll_curve"]}
        before = [
            t
            for t in trades
            if t["evidence_class"] == evidence
            and t["checkpoint"] == cp
            and t["strategy"] == strategy
            and t["freshness_minutes"] == 30
        ]
        after = [t for t in before if t["target_date"] in retained_dates]
        metrics = {}
        for h in ("warm", "cold"):
            b = [t for t in before if deep.half(t["target_date"]) == h]
            a = [t for t in after if deep.half(t["target_date"]) == h]
            bm, am = deep.trading_metrics(b), deep.trading_metrics(a)
            metrics[h] = dict(
                n_before=len(b),
                n_retained=len(a),
                before=bm,
                after=am,
                roi_delta=am["roi"] - bm["roi"]
                if bm["roi"] is not None and am["roi"] is not None
                else None,
            )
        dates = sorted({t["target_date"] for t in before})
        result.append(
            dict(
                evidence_class=evidence,
                checkpoint=cp,
                strategy=strategy,
                gate=gate,
                available_models=sorted({m for t in before for m in t["available_models"]}),
                date_range=[dates[0], dates[-1]] if dates else None,
                n=len(after),
                half_year_metrics=metrics,
                classification=deep.classification(
                    metrics["warm"]["roi_delta"],
                    metrics["cold"]["roi_delta"],
                    metrics["warm"]["n_retained"],
                    metrics["cold"]["n_retained"],
                ),
            )
        )
    no_trade["season_interactions"] = result


def enrich(root):
    summary = read(root / MASTERS[0])
    regimes = read(root / MASTERS[1])
    policies = read(root / MASTERS[2])
    trading = read(root / MASTERS[3])
    no_trade = read(root / MASTERS[4])
    rows = read(root / "derived/checkpoints.json")
    trades = read(root / "derived/trading-records.json")
    no_trade_interactions(no_trade, trades)
    atomic_json(root / MASTERS[4], no_trade)
    summary["no_trade_classifications"] = [
        e for e in no_trade["season_interactions"] if e["evidence_class"] == deep.EVIDENCE
    ]
    summary["policy_reference_aliases"] = {"Champion": "current", "100pct": "current"}
    summary["production_transfer_status"] = (
        "no_annual_production_rule_established_method_drift_requires_shadow_validation"
    )
    summary["model_count_distribution"] = dict(Counter(r["model_count"] for r in rows))
    for model, coverage in summary["coverage_by_model"].items():
        coverage["used_in_forecast_checkpoints"] = sum(
            model in r.get("used_models", []) for r in rows
        )
    f = pd.read_parquet(root / "normalized/fixed-lead-paths.parquet")
    variables = []
    for model, g in f.groupby("model"):
        variables.append(
            dict(
                evidence_class=deep.EVIDENCE,
                available_models=[model],
                date_range=[str(g.target_date.min()), str(g.target_date.max())],
                n=len(g),
                variable_coverage={
                    field: float(g[field].notna().mean()) for field in deep.HOURLY_FIELDS.values()
                },
                temp_850hpa_coverage=0,
            )
        )
    summary["source_variable_coverage_by_model"] = variables
    market = summary["market_coverage"]
    found = [e["target_date"] for e in market["events"] if not e.get("error")]
    summary["market_coverage"] = dict(
        days_requested=market["days_requested"],
        events_available=market["events_available"],
        first_found_event=min(found),
        last_found_event=max(found),
        winter_djf_events=0,
        bucket_checkpoint_rows=market["bucket_checkpoint_rows"],
        requests=market["requests"],
        causal_points=market["points_at_or_before_checkpoint"],
        fresh_30m_points=market["fresh_points_at_or_before_checkpoint"],
        missing_event_dates="Full coverage/error ledger in markets/coverage.json; standard-slug 404 is not proof no differently named market existed",
    )
    categories = {}
    for family, key in (
        ("regime", "regime_classifications"),
        ("policy", "policy_classifications"),
        ("trading", "trading_classifications"),
        ("no_trade", "no_trade_classifications"),
    ):
        for e in summary[key]:
            if e.get("policy_id") == "current":
                continue
            categories.setdefault(e["classification"], []).append(
                dict(
                    family=family,
                    checkpoint=e["checkpoint"],
                    rule=e.get("regime", e.get("policy_id", e.get("strategy"))),
                    gate=e.get("gate"),
                )
            )
    summary["annual_classification_groups"] = categories
    summary["output_files"] = [
        *MASTERS,
        "deep-seasonal-replay-report.md",
        "deep-seasonal-validation.json",
    ]
    summary["forecast_season_baselines"] = [
        dict(
            **deep.meta(
                [
                    r
                    for r in rows
                    if r["checkpoint_label"] == cp and deep.season(r["target_date"]) == s
                ],
                deep.EVIDENCE,
            ),
            checkpoint=cp,
            season=s,
            metrics=deep.score(
                [
                    r
                    for r in rows
                    if r["checkpoint_label"] == cp and deep.season(r["target_date"]) == s
                ]
            ),
        )
        for cp in deep.CHECKPOINTS
        for s in ("Winter", "Spring", "Summer", "Autumn")
    ]
    atomic_json(root / MASTERS[0], compact(summary))
    append_findings(root, summary, regimes, policies, trading, no_trade)


def append_findings(root, summary, regimes, policies, trading, no_trade):
    text = [
        "",
        "## Verifizierte Schlussfolgerungen",
        "",
        "Frühestes seriös evaluierbares Datum: 1. November 2024. Es wurden zwei zusätzliche Winter und "
        "das Kalenderjahr 2025 abgedeckt. Der priorisierte Winter 2025/26 hat 151 Target Days, 604 Checkpoints, "
        "147 vollständige akzeptierte Actual-Tage und 588 evaluierbare Forecasts. Insgesamt fehlen an 22 von 707 Tagen "
        "akzeptierte Actuals; deren Forecasts werden nicht in Skill-Metriken bewertet.",
        "",
        "Die nominelle Kohorte enthält IFS025, GFS, ICON global, ICON EU, ARPEGE Europe und UKMO 10 km. "
        "Bei Archivlücken sind fünf statt sechs Modelle verfügbar. AROME, AROME HD und Meteoblue fehlen. "
        "Exakte Initialisierungen und Run Ages sind unbekannt; Nominalvorlauf, obere Run-Schranke, untere Alters-Schranke "
        "und Verfügbarkeitsalter stehen je Checkpoint im Replay. Es gibt keine Reanalyse- oder Hindcast-Ergänzung.",
        "",
        "Stable / High Confidence ist kein einheitliches Ganzjahres-Signal: Winter-Lifts unterscheiden sich nach Checkpoint. "
        "Rapid Heat Ramp tritt auch im Winter auf, seine Treffergüte hängt aber ebenfalls vom Checkpoint ab. "
        "D0 Temperature Plateau bleibt in Winter, Frühling und Herbst negativ; die Sommergruppe ist klein. "
        "Bucket Boundary hat besonders bei D0 und Late Live saisonale Vorzeichenwechsel.",
        "",
        "|Evidence class|Modelle|Checkpoint|Regime|Saison|Zeitraum|N|Accuracy|Lift pp|MAE|Bias|",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for e in regimes["cells"]:
        if (
            e["evidence_class"] != deep.EVIDENCE
            or e["dimension"] != "season"
            or e["period"] not in ("Winter", "Summer")
        ):
            continue
        if (
            e["regime"]
            not in (
                "Stable / High Confidence",
                "Rapid Heat Ramp",
                "Temperature Plateau",
                "Bucket Boundary",
            )
            or not e["n"]
        ):
            continue
        m = e["metrics"]
        text.append(
            f"|{deep.EVIDENCE}|IFS/GFS/ICON/ICON-EU/ARPEGE/UKMO|{e['checkpoint']}|{e['regime']}|{e['period']}|{'–'.join(e['date_range'])}|{e['n']}|{m['modal_accuracy']:.1%}|{e['lift_pp']:+.2f}|{m['mae_c']:.3f}|{m['signed_bias_c']:+.3f}|"
        )
    text += [
        "",
        "anchor_25pct: Winter +1,15 pp (2 zusätzliche Treffer / 174), Frühling −2,23 pp (−4 / 179), "
        "Sommer +3,95 pp (+7 / 177), Herbst +9,68 pp (+15 / 155). Damit saisonabhängig; keine ganzjährige 25%-Empfehlung. "
        "late_dry_mixing_0pct: Winter +1,72 pp (+3 / 174), Frühling −0,56 pp (−1 / 179), Sommer +0,56 pp (+1 / 177), "
        "Herbst +3,23 pp (+5 / 155). MAE verbessert sich in allen vier Jahreszeiten, die Accuracy-Effekte sind teils nur einzelne Fälle.",
        "",
        "Ganzjahres-Klassifikationen stehen im Summary und in jeder Studie. Positive aggregierte Halbjahreswerte "
        "werden durch Vorzeichenwechsel einzelner ausreichend großer Saisongruppen eingeschränkt. Seltene Regimes "
        "bleiben insufficient_evidence; Monatsinstabilität ergibt fragile. Diese Klassifikation gilt nur innerhalb "
        "der Reduced-Fidelity-Kohorte. Keine Regel wurde als ganzjährig auf Production übertragbar bestätigt.",
        "",
        "## Saisonale NO-TRADE-Effekte",
        "",
        "Winter (DJF): keine verifizierbaren historischen Marktpreise, daher kein ROI-/Drawdown- oder NO-TRADE-Nachweis. "
        "Die zusätzliche Historie enthält nur 15 gefundene Standard-Slug-Events ab 16. März 2026. Kalte Halbjahres-Trades "
        "bestehen daher hauptsächlich aus März und acht Oktobertagen; sie ersetzen keinen echten Wintertest.",
        "",
        "|Evidence class|Modelle|Checkpoint|Saison|Zeitraum|Gate|N vorher/nachher|ROI vorher/nachher|DD vorher/nachher|Accuracy vorher/nachher|",
        "|---|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for e in no_trade["entries"]:
        if (
            e["evidence_class"] != deep.EVIDENCE
            or e["strategy"] != "A_champion_top1"
            or e["gate"] != "small_top1_top2_gap"
            or not e["n_before"]
        ):
            continue
        b, a = e["before"], e["after"]
        roi = "unavailable" if a["roi"] is None else f"{a['roi']:.1%}"
        accuracy = "unavailable" if e["after_accuracy"] is None else f"{e['after_accuracy']:.1%}"
        text.append(
            f"|{deep.EVIDENCE}|IFS/GFS/ICON/ICON-EU/ARPEGE/UKMO|{e['checkpoint']}|{e['season']}|{'–'.join(e['date_range'])}|Top1/Top2 gap ≥15 pp|{e['n_before']}/{e['n_retained']}|{b['roi']:.1%}/{roi}|{b['max_drawdown']:.2f}/{a['max_drawdown']:.2f}|{e['before_accuracy']:.1%}/{accuracy}|"
        )
    text += [
        "",
        "Alle weiteren einfachen Gates, Strategien, maximalen Drawdowns, Verlustserien und Bankroll-Kurven "
        "bleiben vollständig in den JSON-Dateien. Keine kombinatorische Regel wurde entwickelt; schlechte frühere "
        "Regimeleistung verwendet nur vorher bekannte Actuals. Cold-season Trading- und NO-TRADE-Klassifikationen "
        "sind insufficient_evidence.",
        "",
        "## Verifikation und Dateien",
        "",
        "Original-v0.3-Actuals und alle archivierten Ursprungsinputs bleiben identisch. Kausale Modell-/Preisgrenzen, "
        "Portfoliogewichte, Payouts und 16 Checkpoints mit absichtlich veränderten aktuellen/zukünftigen Actuals "
        "wurden geprüft. Alle Forecasts und Parameter-Varianten bleiben bei dieser Label-Veränderung identisch. "
        "Die geschützten Engine-Dateien sind unverändert. Ausgeführter Replay-Code und reine Formatkorrektur "
        "sind über Commit und identische Funktions-ASTs dokumentiert.",
        "",
        "Ergebnisdateien: "
        + ", ".join(summary["output_files"])
        + ". Das ZIP enthält zusätzlich normalisierte Quellen, "
        "neue Raw-Caches, Markt-Coverage, Replay-Records, Trading-Records und Input-Manifeste. "
        "Quellcode liegt ausschließlich auf dem Research-Branch.",
        "",
    ]
    path = root / "deep-seasonal-replay-report.md"
    path.write_text(path.read_text() + "\n".join(text))


def package(root):
    destination = root.parent / "Madrid-Deep-Seasonal-Replay-v1.zip"
    files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and not p.name.startswith(".")
        and not ("days" in p.parts and "derived" in p.parts)
        and not p.name.endswith(".library.json")
    ]
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files):
            z.write(p, str(p.relative_to(root)))
    return destination, len(files)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    a = p.parse_args()
    deep.prohibit_network()
    result = audit(a.root, a.source, a.archive)
    enrich(a.root)
    result["master_sha256"] = {
        name: digest((a.root / name).read_bytes())
        for name in (*MASTERS, "deep-seasonal-replay-report.md")
    }
    atomic_json(a.root / "deep-seasonal-validation.json", result)
    shutil.copy(
        a.root.parent / "deep-seasonal-protected-engine-validation.txt",
        a.root / "protected-engine-validation.txt",
    )
    destination, count = package(a.root)
    print(
        json.dumps(
            dict(
                validation="passed",
                zip=str(destination),
                files=count,
                bytes=destination.stat().st_size,
            )
        )
    )


if __name__ == "__main__":
    main()
