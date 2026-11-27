"""Frozen historical experiment. Optional scientific dependencies, offline only."""
from collections import defaultdict
import hashlib
import json
from math import sqrt
from pathlib import Path
from statistics import mean, pstdev

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .contracts import (
    distribution, normal_buckets, observed_floor, require_training_labels, score,
    temperature_scale,
)
from .evidence import FEATURES, MODELS


METRICS = ("top1", "top2", "mae", "bias", "brier", "log_loss", "discrete_crps",
           "winner_probability", "zero_winner_mass", "coverage80", "width80")
CONFIG = {
    "schema": "v11-research-0.1", "research_only": True, "automatic_promotion": False,
    "automatic_execution": False, "airport": "LEMD", "feature_version": "v11_features_1",
    "discovery": ["2024-11-01", "2025-08-31"],
    "calibration": ["2025-09-01", "2025-10-31"],
    "historical_validation": ["2025-11-01", "2026-10-08"],
    "diagnostic_daily": ["2026-10-11", "2026-11-20"],
    "temperature_grid": [.7, 1.0, 1.3, 1.6, 2.0],
    "spread_grid": [1.0, 1.25, 1.5, 2.0],
    "min_training_rows": 60, "max_observation_age_minutes": 45,
    "observation_imputation": "training_medians_with_missingness_indicators",
    "direct_delta": {"C": .1, "tail_pseudocount": .25},
    "selection": "calibration log-loss minimum; no validation/daily-driven refit",
    "evidence_limit": "retrospective historical experiment; not certified causal or OOS",
}


def design(rows: list[dict], feature_names=FEATURES):
    return np.array([[r["features"].get(k) if r["features"].get(k) is not None else np.nan
                      for k in feature_names] for r in rows], dtype=float)


def feature_set(mode):
    if mode == "solar":
        return tuple(k for k in FEATURES if not k.startswith("wind") and k != "dewpoint_change")
    if mode == "no_solar":
        return tuple(k for k in FEATURES if "radiation" not in k)
    return FEATURES


def summarize(records: list[dict]) -> dict:
    if not records:
        return {"n": 0}
    out = {"n": len(records), **{k: mean(r[k] for r in records) for k in METRICS}}
    bins = []
    for i in range(10):
        part = [r for r in records if min(int(r["confidence"] * 10), 9) == i]
        if part:
            bins.append({"lower": i / 10, "n": len(part),
                         "predicted": mean(r["confidence"] for r in part),
                         "observed": mean(r["top1"] for r in part)})
    out["top_label_ece"] = sum(b["n"] * abs(b["predicted"] - b["observed"]) for b in bins) / len(records)
    out["reliability"] = bins
    live = [r for r in records if r.get("actual_delta") is not None]
    if live:
        out["missed_later_touches"] = sum(r["actual_delta"] > 0 and r["modal_delta"] == 0 for r in live)
        out["false_higher_bucket"] = sum(r["actual_delta"] == 0 and r["modal_delta"] > 0 for r in live)
    return out


class FrozenModel:
    """Each instance fits only one airport/checkpoint on the discovery subset."""
    def __init__(self, train: list[dict]):
        require_training_labels(train, CONFIG["calibration"][0] + "T00:00:00+00:00")
        self.train = train
        self.mu = np.array([mean(r["models"][m] for m in MODELS) for r in train])
        self.var = np.array([pstdev([r["models"][m] for m in MODELS]) ** 2 for r in train])
        self.y = np.array([r["actual"] for r in train])
        self.errors = {m: np.array([r["actual"] - r["models"][m] for r in train]) for m in MODELS}
        self.weights = {}
        for mode in ("inverse_mae", "log_score", "brier_score"):
            weights = {}
            for m, errors in self.errors.items():
                if mode == "inverse_mae":
                    loss = np.abs(errors).mean()
                else:
                    scores = [score(normal_buckets(r["models"][m], max(.35, float(np.std(errors)))), r["actual"])
                              for r in train]
                    loss = mean(x["log_loss" if mode == "log_score" else "brier"] for x in scores)
                weights[m] = 1 / max(.05, loss)
            total = sum(weights.values())
            self.weights[mode] = {m: w / total for m, w in weights.items()}
        self.best_single = min(MODELS, key=lambda m: float(np.abs(self.errors[m]).mean()))
        for dim in ("season", "regime"):
            for label in sorted({r[dim] for r in train}):
                part = [r for r in train if r[dim] == label]
                if len(part) >= 30:
                    weights = {m: 1 / max(.05, mean(abs(r["actual"] - r["models"][m]) for r in part)) for m in MODELS}
                    total = sum(weights.values())
                    self.weights[dim + ":" + label] = {m: v / total for m, v in weights.items()}
        def objective(params):
            a, b, c, d = params
            location = a + b * self.mu
            sigma = np.sqrt(c + d * self.var)
            probs = ndtr((self.y + .5 - location) / sigma) - ndtr((self.y - .5 - location) / sigma)
            return -np.log(np.maximum(probs, 1e-12)).mean()
        fit = minimize(objective, [0, 1, .5, 1], method="L-BFGS-B",
                       bounds=[(-10, 10), (.5, 1.5), (.04, 25), (0, 10)])
        if not fit.success or not np.isfinite(fit.fun):
            raise ValueError("EMOS optimizer did not converge: " + str(fit.message))
        self.emos = list(map(float, fit.x))
        self.live_train = [r for r in train if is_live(r)]
        self.classifiers = {}
        self.tail = {}
        if len(self.live_train) >= CONFIG["min_training_rows"]:
            delta = np.array([r["actual"] - r["observed_max"] for r in self.live_train], dtype=int)
            if np.any(delta < 0):
                raise ValueError("Final maximum below observed maximum")
            self.max_delta = max(8, int(delta.max()) + 2)
            counts = np.array([np.sum(delta == k) + .25 for k in range(2, self.max_delta + 1)])
            self.tail = {k: float(v / counts.sum()) for k, v in zip(range(2, self.max_delta + 1), counts)}
            self.delta_counts = np.bincount(np.minimum(delta, self.max_delta), minlength=self.max_delta + 1) + .25
            for mode in ("full", "solar", "no_solar"):
                names = feature_set(mode)
                classifier = make_pipeline(SimpleImputer(add_indicator=True, keep_empty_features=True),
                                           StandardScaler(), LogisticRegression(C=.1, max_iter=2000))
                if len(set(np.minimum(delta, 2))) >= 2:
                    classifier.fit(design(self.live_train, names), np.minimum(delta, 2))
                    self.classifiers[mode] = classifier

    def metadata(self):
        return {"training_rows": len(self.train), "emos": self.emos,
                "weights": self.weights, "best_discovery_single": self.best_single,
                "training_max_day": max(r["target_date"] for r in self.train),
                "direct_delta_training_rows": len(self.live_train),
                "feature_names": list(FEATURES), "delta_tail": self.tail,
                "classifiers": {mode: {
                    "features": feature_set(mode),
                    "classes": pipeline[-1].classes_.tolist(),
                    "coefficients": pipeline[-1].coef_.tolist(),
                    "intercept": pipeline[-1].intercept_.tolist(),
                    "imputation_medians": pipeline[0].statistics_.tolist(),
                    "missingness_indicator_columns": pipeline[0].indicator_.features_.tolist(),
                    "scaler_mean": pipeline[1].mean_.tolist(),
                    "scaler_scale": pipeline[1].scale_.tolist(),
                } for mode, pipeline in self.classifiers.items()}}

    def center(self, row, mode):
        if mode == "equal":
            return mean(row["models"][m] for m in MODELS)
        if mode.startswith("single:"):
            return row["models"][mode.split(":", 1)[1]]
        weights = self.weights.get(mode, self.weights["inverse_mae"])
        if mode == "seasonal":
            weights = self.weights.get("season:" + row["season"], weights)
        if mode == "regime":
            weights = self.weights.get("regime:" + row["regime"], weights)
        if mode == "freshness":
            weights = {m: weights[m] / (1 + row["model_ages"].get(m, 1440) / 720) for m in MODELS}
        if mode == "champion_weights":
            weights = {m: row["model_weights"].get(m, 0) for m in MODELS}
            if not sum(weights.values()):
                return None
        return sum(weights[m] * row["models"][m] for m in MODELS) / sum(weights.values())

    def empirical(self, row, mode):
        center = self.center(row, mode)
        if center is None:
            return None
        train = self.train
        if mode == "seasonal":
            subset = [r for r in train if r["season"] == row["season"]]
            if len(subset) >= 30:
                train = subset
        if mode == "regime":
            subset = [r for r in train if r["regime"] == row["regime"]]
            if len(subset) >= 30:
                train = subset
        residuals = np.array([r["actual"] - self.center(r, mode) for r in train])
        # Deterministic empirical residual convolution, not invented ensemble members.
        lo = int(center + float(residuals.min())) - 4
        hi = int(center + float(residuals.max())) + 4
        support = np.arange(lo, hi + 1)
        cdfs = ndtr((support[:, None] + .5 - center - residuals[None, :]) / .35).mean(axis=1)
        cdfs[-1] = 1
        probs = np.diff(np.r_[0, cdfs])
        return distribution(dict(zip(support, probs)))

    def predict(self, row):
        values = [row["models"][m] for m in MODELS]
        mu, var = mean(values), pstdev(values) ** 2
        a, b, c, d = self.emos
        result = {"champion": distribution(row["champion"]),
                  "equal_raw": normal_buckets(mu, max(.35, sqrt(var)))}
        for mode in ("equal", "inverse_mae", "log_score", "brier_score", "freshness",
                     "seasonal", "regime", "champion_weights"):
            p = self.empirical(row, mode)
            if p is not None:
                result[mode + "_residual"] = p
        for m in MODELS:
            result["single:" + m] = normal_buckets(row["models"][m] + float(self.errors[m].mean()),
                                                  max(.35, float(self.errors[m].std())))
        result["best_discovery_single"] = result["single:" + self.best_single]
        for spread in CONFIG["spread_grid"]:
            result["emos_spread:" + str(spread)] = normal_buckets(a + b * mu, sqrt(c + d * var) * spread)
        if is_live(row):
            observed = row["observed_max"]
            # Explicit prior/posterior ablation: floor never removes and renormalizes mass.
            for key in list(result):
                if key != "champion":
                    result[key + ":floor"] = observed_floor(result[key], observed)
            result["observed_max"] = {observed: 1.0}
            rest = row["features"].get("remaining_model_rise")
            if rest is not None:
                result["observed_plus_rest"] = normal_buckets(observed + max(0, rest), .35)
                result["observed_plus_rest"] = observed_floor(result["observed_plus_rest"], observed)
            if self.tail:
                counts = self.delta_counts
                result["hour_hold_rate"] = {observed + i: float(v / counts.sum()) for i, v in enumerate(counts)}
                for mode, classifier in self.classifiers.items():
                    probs = classifier.predict_proba(design([row], feature_set(mode)))[0]
                    categories = dict(zip(classifier.classes_, probs))
                    result["delta_" + mode] = {observed: float(categories.get(0, 0)),
                                              observed + 1: float(categories.get(1, 0)),
                                              **{observed + k: float(categories.get(2, 0) * v) for k, v in self.tail.items()}}
        for key, value in row.get("variants", {}).items():
            result["existing:" + key] = distribution(value)
        return result


def is_live(row):
    return (("First Live" in row["checkpoint"] or "Late Live" in row["checkpoint"]) and
            row.get("observed_max") is not None and
            row["features"].get("observation_age", 1e9) <= CONFIG["max_observation_age_minutes"])


def common(row):
    return all(m in row["models"] for m in MODELS)


def fit_models(rows):
    train = [r for r in rows if CONFIG["discovery"][0] <= r["target_date"] <= CONFIG["discovery"][1]
             and common(r) and r["cohort"] == "deep"]
    models = {}
    for cp in sorted({r["checkpoint"] for r in train}):
        subset = [r for r in train if r["checkpoint"] == cp]
        if len(subset) < CONFIG["min_training_rows"]:
            continue
        models[cp] = FrozenModel(subset)
    return models


def evaluate(rows, models):
    records, vectors = [], []
    skipped = defaultdict(int)
    for row in rows:
        if row["target_date"] < CONFIG["calibration"][0]:
            continue
        if row["checkpoint"] not in models:
            skipped["missing_training_checkpoint"] += 1
            continue
        phase = "calibration" if row["target_date"] <= CONFIG["calibration"][1] else (
            "historical_validation" if row["cohort"] == "deep" else "daily_diagnostic")
        if common(row):
            predicted = models[row["checkpoint"]].predict(row)
        else:
            skipped["missing_common_models"] += 1
            predicted = {"champion": row["champion"]}
            if is_live(row):
                predicted["observed_max"] = {row["observed_max"]: 1.0}
        for name, values in predicted.items():
            values = distribution(values)
            s = score(values, row["actual"])
            # Champion exported continuous center is authoritative; distribution
            # expectation can differ after floor conditioning. Keep both explicitly.
            s["probability_mean_mae"] = s["mae"]
            if name == "champion":
                s["mae"] = abs(row["champion_center"] - row["actual"])
                s["bias"] = row["champion_center"] - row["actual"]
            rec = {"day": row["target_date"], "checkpoint": row["checkpoint"], "candidate": name,
                   "phase": phase, "evidence_class": row["evidence_class"], "season": row["season"],
                   "regime": row["regime"], "actual": row["actual"], **s}
            if is_live(row):
                rec.update(actual_delta=row["actual"] - row["observed_max"],
                           modal_delta=s["modal"] - row["observed_max"])
            records.append(rec)
            vectors.append({"day": row["target_date"], "checkpoint": row["checkpoint"],
                            "phase": phase, "candidate": name, "probabilities": values})
    return records, vectors, dict(skipped)


def run(rows: list[dict], output: Path):
    models = fit_models(rows)
    records, vectors, skipped = evaluate(rows, models)
    # Fit temperature on the separate past calibration block and freeze it before
    # inspecting validation. Selection and forecast inference never see market data.
    temps = {}
    lookup = {(r["target_date"], r["checkpoint"]): r for r in rows}
    for cp in models:
        cal = [v for v in vectors if v["checkpoint"] == cp and v["phase"] == "calibration" and v["candidate"] == "delta_full"]
        if cal:
            require_training_labels([lookup[v["day"], cp] for v in cal],
                                    CONFIG["historical_validation"][0] + "T00:00:00+00:00")
            temps[cp] = min(CONFIG["temperature_grid"], key=lambda t: mean(
                score(temperature_scale(v["probabilities"], t), lookup[v["day"], cp]["actual"])["log_loss"] for v in cal))
    for v in list(vectors):
        if v["candidate"] != "delta_full" or v["checkpoint"] not in temps:
            continue
        cp = v["checkpoint"]
        values = temperature_scale(v["probabilities"], temps[cp])
        row = lookup[v["day"], cp]
        s = score(values, row["actual"])
        records.append({"day": v["day"], "checkpoint": cp, "candidate": "delta_calibrated",
                        "phase": v["phase"], "evidence_class": row["evidence_class"],
                        "season": row["season"], "regime": row["regime"], "actual": row["actual"], **s,
                        "actual_delta": row["actual"] - row["observed_max"],
                        "modal_delta": s["modal"] - row["observed_max"]})
        vectors.append({**v, "candidate": "delta_calibrated", "probabilities": values})
    groups = defaultdict(list)
    for r in records:
        groups[r["phase"], r["checkpoint"], r["evidence_class"], r["candidate"]].append(r)
    summaries = [{"phase": phase, "checkpoint": cp, "evidence_class": evidence,
                  "candidate": candidate, **summarize(part)}
                 for (phase, cp, evidence, candidate), part in sorted(groups.items())]
    selected = {}
    # Selection among live or prior candidates uses identical calibration IDs.
    for cp in models:
        cal = [r for r in summaries if r["phase"] == "calibration" and r["checkpoint"] == cp]
        count = max((r["n"] for r in cal), default=0)
        eligible = [r for r in cal if r["n"] == count and r["candidate"] != "champion" and
                    not r["candidate"].startswith("existing:")]
        if eligible:
            selected[cp] = min(eligible, key=lambda r: r["log_loss"])["candidate"]
    slices = []
    for dim in ("season", "regime"):
        sliced = defaultdict(list)
        for r in records:
            if r["candidate"] in {"champion", selected.get(r["checkpoint"]), "delta_full", "observed_max"}:
                sliced[r["phase"], r["checkpoint"], r["evidence_class"], r["candidate"], r[dim]].append(r)
        slices.extend({"dimension": dim, "phase": p, "checkpoint": c, "evidence_class": e,
                       "candidate": n, "slice": s, **summarize(rs)} for (p, c, e, n, s), rs in sorted(sliced.items()))
    paired = []
    by_group = defaultdict(dict)
    for r in records:
        by_group[r["phase"], r["checkpoint"], r["evidence_class"]].setdefault(r["candidate"], {})[r["day"]] = r
    for (phase, cp, evidence), candidates in sorted(by_group.items()):
        for candidate, items in sorted(candidates.items()):
            for reference in ("champion", "observed_max"):
                if reference == candidate or reference not in candidates:
                    continue
                days = sorted(set(items) & set(candidates[reference]))
                differences = np.array([candidates[reference][d]["brier"] - items[d]["brier"] for d in days])
                rng = np.random.default_rng(1101)
                intervals = np.quantile(rng.choice(differences, size=(500, len(days))).mean(axis=1), [.025, .975])
                paired.append({"phase": phase, "checkpoint": cp, "evidence_class": evidence,
                               "candidate": candidate, "reference": reference, "days": days,
                               "candidate_metrics": summarize([items[d] for d in days]),
                               "reference_metrics": summarize([candidates[reference][d] for d in days]),
                               "brier_gain_bootstrap95": list(map(float, intervals))})
    report = {"config": CONFIG, "status": "NO RELEASE CANDIDATE", "skipped": skipped,
              "frozen_selection": selected, "temperature": temps, "summaries": summaries,
              "slices": slices, "paired": paired,
              "trained_models": {cp: m.metadata() for cp, m in models.items()},
              "blockers": ["Historical run publication/reception not certified",
                           "Daily observation reception unverified; diagnostics only",
                           "No untouched sequential v11 OOS; zero prospective days",
                           "No verified matched best ask/spread/depth/fees/official resolutions",
                           "No member-level ensemble history in supplied artifacts",
                           "Protected baseline exact historical runtime absent; deep Champion is reduced fidelity"]}
    output.mkdir(parents=True, exist_ok=True)
    from .evidence import write_rows
    write_rows(output / "paired_comparisons.jsonl.gz", report.pop("paired"))
    report["paired_artifact"] = "paired_comparisons.jsonl.gz"
    (output / "results.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    write_rows(output / "predictions.jsonl.gz", vectors)
    import csv
    with (output / "scores.csv").open("w") as f:
        fields = sorted({k for r in records for k in r})
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(records)
    frozen = {"config": CONFIG, "trained_models": report["trained_models"],
              "selected": selected, "temperature": temps,
              "normalized_data_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
    (output / "model_config.json").write_text(json.dumps(frozen, indent=2) + "\n")
    # joblib is a local replay convenience only; never load an untrusted model artifact.
    import joblib
    joblib.dump(models, output / "fitted_models.joblib", compress=3)
    report["config_sha256"] = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    (output / "results.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report
