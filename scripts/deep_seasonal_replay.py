"""Isolated fixed-lead seasonal research; no database or production writes."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import statistics
import types
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from weatherman.historical_archive import (
    RawCache, ArchiveError, atomic_json, digest, metars, actuals_from_metars, HOURLY_FIELDS, utc,
    eligible as native_eligible,
)
from weatherman.historical_enrichment import PREVIOUS_URL, fetch_ogimet
from weatherman.historical_replay import build_checkpoint, clean, schedule, engine_fingerprint
from weatherman.historical_analysis import (
    analysis_engine, prepared_builders, _RUNTIME, no_parameter_effect,
    context, prediction, valid, score, sample_class, REGIMES, CHECKPOINTS,
)
from weatherman.historical_analysis_reports import (
    tags, comparison, market_view, strategy_legs, execute_price_trade, trading_metrics,
)
from weatherman.model_freshness import assess_model_freshness, ModelFreshnessProfile
from weatherman.decision import DayStatus

EVIDENCE = "deep_seasonal_reduced_fidelity"
ORIGINAL = "historical_partial_fidelity_2026"
FLAGS = dict(research_only=True, automatic_promotion=False, certified_oos=False,
             production_reads=0, production_writes=0, production_neon_backfill=False)
MODELS = ("ecmwf_ifs025", "gfs_global", "icon_global", "icon_eu",
          "ukmo_global_deterministic_10km", "meteofrance_arpege_europe")
START, END = date(2024, 11, 1), date(2026, 10, 8)
INPUT_START = START - timedelta(days=92)
REPO = Path(__file__).resolve().parents[1]
AIRPORT = json.loads((REPO / "src/weatherman/data/airports.json").read_text())["LEMD"]
SPECS = {
    CHECKPOINTS[0]: [("current", {})],
    CHECKPOINTS[1]: [("current", {})] + [
        ("anchor_disabled" if w == 0 else f"anchor_{int(w*100)}pct",
         {"temperature_anchor": w}) for w in (0, .25, .5, .75)],
    CHECKPOINTS[2]: [("current", {})],
    CHECKPOINTS[3]: [("current", {})] + [
        (f"late_dry_mixing_{int(w*100)}pct", {"late_dry_mixing": w})
        for w in (0, .25, .5, .75)],
}


def parquet(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def days(start=START, end=END):
    return [start + timedelta(days=i) for i in range((end-start).days+1)]


def acquire(root, archive, source):
    cache = RawCache(root, max_requests=100, retry_failed=True)
    errors = []
    try:
        # Download only new source requests. Range hashes and raw bytes are immutable.
        frames = []
        for start in days(INPUT_START, END)[::90]:
            end = min(start + timedelta(days=89), END)
            path = root / f"normalized/previous/{start}_{end}.parquet"
            if path.exists():
                frames.append(pd.read_parquet(path)); continue
            print(f"Previous Runs {start}..{end}", flush=True)
            data, meta = cache.get(PREVIOUS_URL, dict(
                latitude=AIRPORT["latitude"], longitude=AIRPORT["longitude"],
                models=",".join(MODELS), start_date=str(start), end_date=str(end),
                hourly=",".join(f"{v}_previous_day{lead}" for lead in (1,2)
                                for v in HOURLY_FIELDS), timezone="UTC"), kind="json")
            h = json.loads(data)["hourly"]
            times = pd.to_datetime(h["time"], utc=True)
            parts = []
            for model in MODELS:
                for lead in (1,2):
                    f = pd.DataFrame({field: h.get(f"{var}_previous_day{lead}_{model}",
                                                  [None]*len(times))
                                      for var, field in HOURLY_FIELDS.items()})
                    f["valid_at"], f["model"], f["lead_days"] = times, model, lead
                    f["source_sha256"], f["fetched_at"] = meta["sha256"], meta["fetched_at"]
                    f["source"], f["airport"] = "open-meteo", "LEMD"
                    f["target_date"] = f.valid_at.dt.tz_convert("Europe/Madrid").dt.date
                    # This is a bound on the contributing run time, not an observed initialization.
                    f["model_run_upper_bound_at"] = f.valid_at - pd.Timedelta(days=lead)
                    f["model_run_at"] = pd.NaT
                    f["temp_850hpa_c"] = None
                    parts.append(f)
            frame = pd.concat(parts, ignore_index=True)
            parquet(path, frame); frames.append(frame)
        combined = pd.concat(frames, ignore_index=True).drop_duplicates(
            ["model", "lead_days", "valid_at"])
        parquet(root / "normalized/fixed-lead-paths.parquet", combined)
        obs_path = root / "normalized/metars.parquet"
        if not obs_path.exists():
            print("METAR: new dates only; existing 2026 archive reused", flush=True)
            obs = metars(cache, INPUT_START - timedelta(days=1), date(2026,1,1))
            old_paths = sorted((source / "normalized").glob("*-metar.parquet"))
            old = pd.concat([pd.read_parquet(p) for p in old_paths], ignore_index=True)
            obs = pd.concat([obs, old], ignore_index=True).sort_values("observed_at")
            obs = obs.drop_duplicates("observed_at", keep="first")
            parquet(obs_path, obs)
        observations = pd.read_parquet(obs_path)
        gap = root / "normalized/metar-2026-jan-mar.parquet"
        if not gap.exists():
            missing = metars(cache, date(2026,1,1), date(2026,3,29))
            parquet(gap, missing)
        observations = pd.concat([observations,pd.read_parquet(gap)],ignore_index=True)
        observations = observations.sort_values("observed_at").drop_duplicates("observed_at",keep="first")
        parquet(obs_path, observations)
        parquet(root / "normalized/actuals.parquet", actuals_from_metars(observations))
        taf_path = root / "normalized/tafs.parquet"
        parts=[]
        import httpx
        cache.client.timeout=httpx.Timeout(75)
        months=list(pd.date_range("2024-11-01","2026-02-01",freq="MS"))
        months.sort(key=lambda x:(not date(2025,11,1)<=x.date()<=date(2026,2,1),x))
        for stamp in months:
            start,end=stamp.date(),(stamp+pd.offsets.MonthBegin()).date()
            p=root / f"normalized/taf-{start}_{end}.parquet"
            if p.exists():
                parts.append(pd.read_parquet(p));continue
            print(f"TAF {start}..{end}",flush=True)
            try:
                t=fetch_ogimet(cache,start,end)
                if not t.empty:
                    parquet(p,t);parts.append(t)
            except ArchiveError as e:
                errors.append(dict(source="ogimet",start=str(start),end=str(end),error=str(e)))
        parts.append(pd.read_parquet(archive / "normalized/ogimet-tafs.parquet"))
        tafs=pd.concat(parts,ignore_index=True).drop_duplicates(["issue_time","content_hash"])
        parquet(taf_path,tafs)
        atomic_json(root / "acquisition.json", dict(
            **FLAGS, requests=cache.requests, bytes=cache.download_bytes, cache_hits=cache.hits,
            errors=errors, fixed_lead_850hpa_status="unavailable_api_rejects_pressure_level_offset",
            source_docs=["https://open-meteo.com/en/docs/previous-runs-api",
                         "https://open-meteo.com/en/docs/single-runs-api"],
            single_runs_note="Pre-April other models unavailable; ECMWF 9km historical hindcasts not used"))
    finally:
        cache.close()


def fixed_daily(frame):
    output, records = [], []
    for (model, lead, day), group in frame.groupby(["model","lead_days","target_date"]):
        start = pd.Timestamp(day, tz="Europe/Madrid")
        stop = pd.Timestamp(day+timedelta(days=1), tz="Europe/Madrid")
        expected = pd.date_range(start, stop, freq="h", inclusive="left").tz_convert("UTC")
        g = group.drop_duplicates("valid_at").copy()
        if len(expected.difference(pd.DatetimeIndex(g.valid_at))) or g.temp_c.isna().any():
            continue
        bound = g.model_run_upper_bound_at.max()
        known = bound + pd.Timedelta(hours=6,minutes=10)
        g["available_at"] = known
        g["availability_basis"] = "estimated_latest_contributing_run_bound_plus_6h10m"
        g["run_at"] = known
        g["fixed_lead_hours"] = int(lead)*24
        output.append(g)
        r = g.iloc[-1].to_dict()
        r.update(max_temp_c=float(g.temp_c.max()), run_at=known,
                 horizon="D-1", daily_aggregation_method="fixed_lead_variable_run_series",
                 model_run_upper_bound_at=bound, model_run_at=pd.NaT,
                 calibration_evidence="fixed_lead_reduced_research_not_exact_issued_run")
        records.append(r)
    return pd.concat(output,ignore_index=True), pd.DataFrame(records)


def seasonal_freshness():
    # Explicit research-only daily fixed-lead schedule, never pretending a current live run.
    namespace = dict(assess_model_freshness.__globals__)
    namespace["model_freshness_profile"] = lambda *a, **k: ModelFreshnessProfile(1440,90)
    f = types.FunctionType(assess_model_freshness.__code__, namespace,
                           assess_model_freshness.__name__, assess_model_freshness.__defaults__)
    f.__kwdefaults__ = assess_model_freshness.__kwdefaults__
    return f


def deep_engine(params):
    f = analysis_engine(json.dumps(params, sort_keys=True))
    namespace = dict(f.__globals__)
    namespace["assess_model_freshness"] = seasonal_freshness()
    output = types.FunctionType(f.__code__, namespace, f.__name__, f.__defaults__)
    output.__kwdefaults__ = f.__kwdefaults__
    return output


def select_daily(paths, forecast, checkpoint):
    ready = forecast[forecast.available_at <= utc(checkpoint)]
    selected = ready.sort_values("available_at").drop_duplicates(["model","target_date"],keep="last")
    keys = set(zip(selected.model, selected.target_date, selected.lead_days))
    p = paths[[k in keys for k in zip(paths.model,paths.target_date,paths.lead_days)]].copy()
    return p, selected


def deep_eligible(frame, checkpoint, *, policy):
    if frame.empty or "lead_days" not in frame:
        return native_eligible(frame, checkpoint, policy=policy)
    if not frame.lead_days.isin([1,2]).all():
        raise ArchiveError("Unsupported fixed lead")
    known=pd.to_datetime(frame.available_at,utc=True,errors="coerce")
    bound=pd.to_datetime(frame.model_run_upper_bound_at,utc=True,errors="coerce")
    if bound.isna().any() or known.isna().any() or (bound>known).any():
        raise ArchiveError("Unknown or future fixed-lead contributing-run bound")
    # Unknown initialization is retained, never invented. Other native guards remain active.
    selected=native_eligible(frame.drop(columns="model_run_at"),checkpoint,policy=policy)
    selected["model_run_at"]=pd.NaT
    return selected


_DATA = None


def replay_day(day_string):
    root, paths, daily, obs, tafs, actuals, fingerprint = _DATA
    day = date.fromisoformat(day_string)
    saved = root / f"derived/{fingerprint}/days/{day}.json"
    if saved.exists():
        return json.loads(saved.read_text())
    history = daily[(daily.target_date < day) & (daily.target_date >= day-timedelta(days=90))
                    & (daily.lead_days == 2)].copy()
    # Only forecast history available before D-1, with earlier accepted Actuals, trains bias.
    observations = obs[(obs.observed_at >= pd.Timestamp(day-timedelta(days=95),tz="Europe/Madrid"))
                       & (obs.observed_at < pd.Timestamp(day+timedelta(days=1),tz="Europe/Madrid"))]
    aviation = tafs[(tafs.valid_to >= pd.Timestamp(day-timedelta(days=2),tz="Europe/Madrid"))
                   & (tafs.issue_time < pd.Timestamp(day+timedelta(days=1),tz="Europe/Madrid"))]
    candidates = paths[(paths.target_date >= day-timedelta(days=1))
                       & (paths.target_date <= day+timedelta(days=1))]
    day_forecasts = daily[(daily.target_date >= day-timedelta(days=1))
                         & (daily.target_date <= day+timedelta(days=1))]
    rows, prior_terminal = [], None
    for label, cutoff in schedule(day):
        p, f = select_daily(candidates,day_forecasts,cutoff)
        build, candidate_build = prepared_builders()
        # Private preparation adapter: a fixed-lead series has no single model_run_at.
        for fn in (build,candidate_build):
            fn.__globals__["daily_forecasts"] = lambda frame, include_stitched: f.copy(deep=True)
            fn.__globals__["eligible"] = deep_eligible
        kwargs = dict(airport=AIRPORT,target=day,label=label,checkpoint=cutoff,paths=p,
                      observations=observations,tafs=aviation,actuals=actuals,
                      timing_policy="conservative",source_errors=[],history_forecasts=history,
                      snapshot_history=pd.DataFrame(),prior_terminal_status=prior_terminal,
                      nowcast_builder=deep_engine({}),build_challengers=False)
        raw = build(**kwargs)
        capture = dict(_RUNTIME["capture"])
        r = context(raw)
        r.update(evidence_class=EVIDENCE,cohort="deep_seasonal_replay",**FLAGS)
        today = f[f.target_date == day]
        r["available_models"] = sorted(today.model.unique())
        r["missing_models"] = sorted(set(AIRPORT["models"])-set(r["available_models"])) + ["meteoblue"]
        r["model_count"] = len(today)
        r["model_provenance"] = [dict(model=x.model,forecast_lead_hours=int(x.lead_days)*24,
            actual_initialization_at=None,run_age_minutes=None,
            contributing_run_upper_bound_at=x.model_run_upper_bound_at,
            contributing_run_age_lower_bound_minutes=(utc(cutoff)-utc(x.model_run_upper_bound_at)).total_seconds()/60,
            available_at=x.available_at,availability_age_minutes=(utc(cutoff)-utc(x.available_at)).total_seconds()/60,
            data_completeness=1.,source_sha256=x.source_sha256,
            variable_completeness={v:float(p[(p.model==x.model)&(p.target_date==day)][v].notna().mean())
                                   for v in HOURLY_FIELDS.values()}) for x in today.itertuples()]
        r["freshness_contract"] = "research_daily_fixed_lead_cohort_24h_not_production_model_cadence"
        r["champion_exact_reconstruction"] = False
        r["memory_status"] = "unavailable_shadow_memory_no_center_effect"
        r["policies"] = {}
        for name, params in SPECS[label]:
            selected = raw
            if params and raw.get("champion_center_c") is not None and not no_parameter_effect(params,capture):
                selected = candidate_build(**{**kwargs,"nowcast_builder":deep_engine(params)})
            pred = prediction(selected, selected.get("champion_center_c"),
                              selected.get("champion_probabilities",{}), policy=name)
            pred.update(evidence_class=EVIDENCE,source_evidence_class=EVIDENCE,cohort="deep_seasonal_replay",**FLAGS)
            pred["available_models"] = r["available_models"]
            r["policies"][name] = pred
        rows.append(clean(r))
        terminal = raw.get("terminal_state")
        if terminal:
            prior_terminal = DayStatus(**terminal)
    atomic_json(saved, rows)
    return rows


def replay(root, workers):
    global _DATA
    paths,daily = fixed_daily(pd.read_parquet(root / "normalized/fixed-lead-paths.parquet"))
    parquet(root / "normalized/eligible-fixed-lead-paths.parquet", paths)
    parquet(root / "normalized/daily-forecasts.parquet", daily)
    obs,tafs,actuals = [pd.read_parquet(root / f"normalized/{name}.parquet")
                       for name in ("metars","tafs","actuals")]
    inputs={str(root/f"normalized/{n}.parquet"):digest((root/f"normalized/{n}.parquet").read_bytes())
            for n in ("fixed-lead-paths","metars","tafs","actuals")}
    code=engine_fingerprint(REPO)
    code["scripts/deep_seasonal_replay.py"]=digest(Path(__file__).read_bytes())
    fingerprint=digest(json.dumps(dict(inputs=inputs,code=code,specs=SPECS),sort_keys=True).encode())
    atomic_json(root/"replay-input-manifest.json",dict(**FLAGS,fingerprint=fingerprint,inputs=inputs,code=code,
        target_range=[str(START),str(END)],models=list(MODELS),policy_registry=SPECS,
        evidence_class=EVIDENCE,availability="latest contributing run upper bound plus 6h10m",
        freshness="private 24h fixed lead cohort; original production cadence unchanged"))
    _DATA = root,paths,daily,obs,tafs,actuals,fingerprint
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i,result in enumerate(pool.map(replay_day,map(str,days())),1):
            rows.extend(result)
            if i%20==0: print(f"Deep replay {i}/{len(days())} days",flush=True)
    atomic_json(root / "derived/checkpoints.json", rows)
    if any(digest(Path(p).read_bytes())!=sha for p,sha in inputs.items()):
        raise ArchiveError("Replay input changed during execution")


def season(day):
    month=int(str(day)[5:7])
    return "Winter" if month in (12,1,2) else "Spring" if month in (3,4,5) else "Summer" if month in (6,7,8) else "Autumn"


def half(day):
    return "warm" if 4 <= int(str(day)[5:7]) <= 9 else "cold"


def meta(rows, evidence, requested=None):
    dates=sorted({r["target_date"] for r in rows})
    return dict(evidence_class=evidence,
                available_models=sorted({m for r in rows for m in r.get("available_models",[])}),
                available_model_sets=[list(x) for x in sorted({tuple(r.get("available_models",[])) for r in rows})],
                date_range=[dates[0],dates[-1]] if dates else requested,
                n=sum(valid(r) for r in rows), checkpoint_records=len(rows),
                sample_size_class=sample_class(sum(valid(r) for r in rows)))


def classification(warm, cold, warm_n, cold_n, drift=False):
    if warm_n < 30 or cold_n < 30 or warm is None or cold is None:
        return "insufficient_evidence"
    if drift:
        return "fragile"
    if warm > 0 and cold > 0: return "seasonally_robust_positive"
    if warm < 0 and cold < 0: return "robust_negative"
    if warm*cold < 0: return "season_dependent"
    if warm > 0: return "warm_season_positive"
    if cold > 0: return "cold_season_positive"
    return "fragile"


def prohibit_network():
    import socket
    original=socket.socket.connect
    original_ex=socket.socket.connect_ex
    def connect(instance,address):
        if instance.family in (socket.AF_INET,socket.AF_INET6):
            raise RuntimeError("Seasonal replay/analysis is offline")
        return original(instance,address)
    def connect_ex(instance,address):
        if instance.family in (socket.AF_INET,socket.AF_INET6):
            raise RuntimeError("Seasonal replay/analysis is offline")
        return original_ex(instance,address)
    socket.socket.connect=connect
    socket.socket.connect_ex=connect_ex


def regime_study(cohorts):
    cells, interactions = [], []
    for evidence,rows in cohorts.items():
        for cp in CHECKPOINTS:
            base=[r for r in rows if r["checkpoint_label"]==cp and valid(r)]
            for dimension,periods in [("season",("Winter","Spring","Summer","Autumn")),
                                      ("month",sorted({r["target_date"][:7] for r in base})),
                                      ("half_year",("warm","cold"))]:
                for period in periods:
                    selected=[r for r in base if (season(r["target_date"]) if dimension=="season" else
                              half(r["target_date"]) if dimension=="half_year" else r["target_date"][:7])==period]
                    baseline=score(selected)
                    for regime in REGIMES:
                        group=[r for r in selected if regime in tags(r)]
                        s=score(group)
                        lift=100*(s["modal_accuracy"]-baseline["modal_accuracy"]) if group else None
                        cells.append(dict(**meta(group,evidence,meta(selected,evidence)["date_range"]), checkpoint=cp, dimension=dimension,
                            period=period, regime=regime, metrics=s,baseline=baseline,lift_pp=lift,
                            baseline_date_range=meta(selected,evidence)["date_range"],
                            frequency=len(group)/len(selected) if selected else None,
                            mae_delta_c=s["mae_c"]-baseline["mae_c"] if group else None))
            for regime in REGIMES:
                matches=[c for c in cells if c["evidence_class"]==evidence and c["checkpoint"]==cp
                         and c["regime"]==regime and c["dimension"]=="half_year"]
                warm,cold=next(c for c in matches if c["period"]=="warm"),next(c for c in matches if c["period"]=="cold")
                months=[c for c in cells if c["evidence_class"]==evidence and c["checkpoint"]==cp
                        and c["regime"]==regime and c["dimension"]=="month" and c["n"]>=10]
                status=classification(warm["lift_pp"],cold["lift_pp"],warm["n"],cold["n"])
                positive=sum(c["lift_pp"]>0 for c in months)
                negative=sum(c["lift_pp"]<0 for c in months)
                if status=="seasonally_robust_positive" and (len(months)<3 or positive/len(months)<.7):
                    status="fragile"
                interactions.append(dict(**meta(base,evidence),checkpoint=cp,regime=regime,
                    warm_n=warm["n"],cold_n=cold["n"],warm_lift_pp=warm["lift_pp"],cold_lift_pp=cold["lift_pp"],
                    sign_change=bool(warm["lift_pp"] is not None and cold["lift_pp"] is not None
                                     and warm["lift_pp"]*cold["lift_pp"]<0),
                    positive_months=positive,negative_months=negative,months_n_ge10=len(months),
                    classification=status,classification_scope="diagnostic_within_same_fidelity_class_not_oos"))
    return dict(**FLAGS,cells=cells,season_interactions=interactions,
                half_year_definition="warm April--September; cold October--March",
                season_definition="meteorological DJF/MAM/JJA/SON",
                classification_minimum="Both halves N>=30; positive robustness additionally >=3 months and >=70% positive lifts")


def policy_study(cohorts):
    entries, interactions=[],[]
    for evidence,rows in cohorts.items():
        for cp in (CHECKPOINTS[1],CHECKPOINTS[3]):
            base=[r for r in rows if r["checkpoint_label"]==cp and valid(r)]
            for name,_ in SPECS[cp]:
                for dimension,periods in [("season",("Winter","Spring","Summer","Autumn")),
                                          ("month",sorted({r["target_date"][:7] for r in base})),
                                          ("half_year",("warm","cold"))]:
                    for period in periods:
                        selected=[r for r in base if (season(r["target_date"]) if dimension=="season" else
                                   half(r["target_date"]) if dimension=="half_year" else r["target_date"][:7])==period]
                        matched=[r for r in selected if valid(r.get("policies",{}).get(name,{}))]
                        p=[r["policies"][name] for r in matched]
                        cmp=comparison(matched,p) if matched else {"n":0,"status":"unavailable"}
                        entries.append(dict(**meta(matched,evidence,meta(selected,evidence)["date_range"]),checkpoint=cp,policy_id=name,
                                            dimension=dimension,period=period,comparison=cmp))
                matches=[e for e in entries if e["evidence_class"]==evidence and e["checkpoint"]==cp
                         and e["policy_id"]==name and e["dimension"]=="half_year"]
                w,c=next(e for e in matches if e["period"]=="warm"),next(e for e in matches if e["period"]=="cold")
                # comparison reports deltas under delta; retain explicit matched baselines.
                wd=w["comparison"].get("accuracy_delta_pp"); cd=c["comparison"].get("accuracy_delta_pp")
                interactions.append(dict(**meta(base,evidence),checkpoint=cp,policy_id=name,
                    warm_n=w["n"],cold_n=c["n"],warm_accuracy_delta_pp=wd,cold_accuracy_delta_pp=cd,
                    classification=classification(wd,cd,w["n"],c["n"])))
    return dict(**FLAGS,entries=entries,season_interactions=interactions,
                policies="Existing anchor and late dry mixing weights only; current is 100%, Champion alias",
                optimization="none; no parameters fitted to target outcomes")


STRATEGIES=("A_champion_top1","B_champion_top1_top2","C_top2_plus_taf_outside",
            "D_first_live_taf_hedge_10","D_first_live_taf_hedge_20","D_first_live_taf_hedge_30",
            "G_existing_observable_no_trade_gates")


def trade_study(cohorts, price_records):
    markets=defaultdict(list)
    for r in price_records:
        markets[(r["target_date"],r["checkpoint_label"])].append(r)
    for key,value in markets.items():
        if sum(r.get("resolved_yes") is True for r in value)!=1:
            raise ArchiveError(f"Market has no unique resolved winner: {key}")
    rows_by_key={}; trades=[]; poor_cache={}
    for evidence,rows in cohorts.items():
        for r in rows:
            if not valid(r): continue
            rows_by_key[(evidence,r["target_date"],r["checkpoint_label"])]=r
            records=markets.get((r["target_date"],r["checkpoint_label"]),[])
            if not records: continue
            view=market_view(r,records)
            for strategy in STRATEGIES:
                legs=strategy_legs(r,view,strategy)
                for freshness in (10,30,60,"over60_sensitivity_only"):
                    t=execute_price_trade(r,legs,freshness)
                    if t:
                        t.update(evidence_class=evidence, strategy=strategy,freshness_minutes=freshness,
                                 available_models=r["available_models"],**FLAGS)
                        t["cohort"]="deep_seasonal_replay" if evidence==EVIDENCE else "diagnostic_partial_replay"
                        trades.append(t)
    entries, regime_entries, gates=[],[],[]
    for evidence,rows in cohorts.items():
        for cp in CHECKPOINTS:
            forecast=[r for r in rows if r["checkpoint_label"]==cp and valid(r)]
            months=sorted({r["target_date"][:7] for r in forecast})
            for dimension,periods in (("season",("Winter","Spring","Summer","Autumn")),("month",months)):
                for period in periods:
                    base=[r for r in forecast if (season(r["target_date"]) if dimension=="season"
                                                 else r["target_date"][:7])==period]
                    dates={r["target_date"] for r in base}
                    for strategy in STRATEGIES:
                        if strategy.startswith("D_") and cp!=CHECKPOINTS[2]: continue
                        for freshness in (10,30,60,"over60_sensitivity_only"):
                            selected=[t for t in trades if t["evidence_class"]==evidence and t["checkpoint"]==cp
                                      and t["strategy"]==strategy and t["freshness_minutes"]==freshness
                                      and t["target_date"] in dates]
                            group=[rows_by_key[(evidence,t["target_date"],cp)] for t in selected]
                            entry=dict(**meta(group,evidence,meta(base,evidence)["date_range"]),checkpoint=cp,strategy=strategy,
                                dimension=dimension,period=period,freshness_minutes=freshness,
                                metrics=trading_metrics(selected),status="available" if selected else "unavailable_no_eligible_prices")
                            entries.append(entry)
                            if dimension!="season" or freshness!=30: continue
                            for regime in REGIMES:
                                rt=[t for t in selected if regime in t["regimes"]]
                                group=[rows_by_key[(evidence,t["target_date"],cp)] for t in rt]
                                regime_entries.append(dict(**meta(group,evidence,meta(base,evidence)["date_range"]),checkpoint=cp,strategy=strategy,
                                    season=period,regime=regime,metrics=trading_metrics(rt),
                                    eligible_for_conclusion=len(rt)>=20))
                            for gate in ("small_top1_top2_gap","weak_top1_probability","high_model_spread",
                                         "taf_disagreement","stale_price_10m","poor_prior_regime_performance"):
                                retained=[]
                                for t in selected:
                                    row=rows_by_key[(evidence,t["target_date"],cp)]
                                    exclude=False
                                    if gate=="small_top1_top2_gap": exclude=(row.get("top1_top2_gap_pp") or 0)<15
                                    elif gate=="weak_top1_probability": exclude=(row.get("top1_probability") or 0)<.4
                                    elif gate=="high_model_spread": exclude="Model Spread high" in tags(row)
                                    elif gate=="taf_disagreement": exclude="TAF Disagreement" in tags(row)
                                    elif gate=="stale_price_10m": exclude=t["max_price_age_minutes"]>10
                                    else:
                                        key=evidence,row["target_date"],cp
                                        if key not in poor_cache:
                                            prior=[x for x in forecast if x["target_date"]<row["target_date"]
                                                   and x.get("actual_available_at")
                                                   and utc(x["actual_available_at"])<=utc(row["checkpoint_at"])]
                                            rejected=False
                                            if len(prior)>=30:
                                                accuracy=sum(x["champion_modal_bucket"]==x["actual_bucket"] for x in prior)/len(prior)
                                                for regime in tags(row).intersection(REGIMES):
                                                    p=[x for x in prior if regime in tags(x)]
                                                    if len(p)>=20 and sum(x["champion_modal_bucket"]==x["actual_bucket"] for x in p)/len(p)<accuracy:
                                                        rejected=True;break
                                            poor_cache[key]=rejected
                                        exclude=poor_cache[key]
                                    if not exclude: retained.append(t)
                                group=[rows_by_key[(evidence,t["target_date"],cp)] for t in retained]
                                gates.append(dict(**meta(group,evidence,meta(base,evidence)["date_range"]),checkpoint=cp,strategy=strategy,
                                    season=period,gate=gate,n_before=len(selected),n_retained=len(retained),
                                    before=trading_metrics(selected),after=trading_metrics(retained),
                                    before_accuracy=trading_metrics(selected).get("forecast_modal_accuracy"),
                                    after_accuracy=trading_metrics(retained).get("forecast_modal_accuracy"),
                                    freshness_minutes=30,calibration_scope="Single existing gates; no fitted combination"))
    common=dict(**FLAGS,pnl_type="price_based_hypothetical_pnl",price_type="Polymarket archived prices-history series_price",
                executable_quotes=False,fees_slippage_depth="not available; gross hypothetical returns",
                stake="1 unit per eligible checkpoint; fixed 1000-unit bankroll; no compounding",
                drawdown="Chronological target-day settled returns; overlapping checkpoints not pooled")
    interactions=[]
    for evidence,rows in cohorts.items():
        for cp in CHECKPOINTS:
            for strategy in STRATEGIES:
                if strategy.startswith("D_") and cp!=CHECKPOINTS[2]:continue
                selected=[t for t in trades if t["evidence_class"]==evidence and t["checkpoint"]==cp
                          and t["strategy"]==strategy and t["freshness_minutes"]==30]
                w=[t for t in selected if half(t["target_date"])=="warm"]
                c=[t for t in selected if half(t["target_date"])=="cold"]
                wm,cm=trading_metrics(w),trading_metrics(c)
                group=[rows_by_key[(evidence,t["target_date"],cp)] for t in selected]
                interactions.append(dict(**meta(group,evidence),checkpoint=cp,strategy=strategy,
                    warm=wm,cold=cm,classification=classification(wm.get("roi"),cm.get("roi"),len(w),len(c)),
                    diagnostic_only=True))
    return dict(**common,entries=entries,regime_entries=regime_entries,season_interactions=interactions),dict(**common,entries=gates),trades


def fidelity_comparison(deep, original):
    lookup={(r["target_date"],r["checkpoint_label"]):r for r in original}
    entries=[]
    for cp in CHECKPOINTS:
        paired=[(lookup[(r["target_date"],cp)],r) for r in deep if r["checkpoint_label"]==cp
                and (r["target_date"],cp) in lookup and valid(r) and valid(lookup[(r["target_date"],cp)])]
        for dimension,periods in (("all",("all",)),("month",sorted({a["target_date"][:7] for a,b in paired}))):
            for period in periods:
                pairs=[(a,b) for a,b in paired if dimension=="all" or a["target_date"][:7]==period]
                a,b=[x for x,y in pairs],[y for x,y in pairs]
                if not pairs:continue
                delta=[y["champion_center_c"]-x["champion_center_c"] for x,y in pairs]
                agreement=sum(x["champion_modal_bucket"]==y["champion_modal_bucket"] for x,y in pairs)/len(pairs)
                entries.append(dict(evidence_classes=[ORIGINAL,EVIDENCE],checkpoint=cp,dimension=dimension,period=period,
                    n=len(pairs),date_range=meta(b,EVIDENCE)["date_range"],
                    available_models={ORIGINAL:meta(a,ORIGINAL)["available_models"],EVIDENCE:meta(b,EVIDENCE)["available_models"]},
                    center_delta_mean_c=statistics.fmean(delta),center_delta_mae_c=statistics.fmean(map(abs,delta)),
                    center_delta_max_absolute_c=max(map(abs,delta)),modal_agreement=agreement,
                    exact_regime_set_agreement=sum(tags(x).intersection(REGIMES)==tags(y).intersection(REGIMES)
                                                  for x,y in pairs)/len(pairs),
                    regime_agreement={regime:sum((regime in tags(x))==(regime in tags(y)) for x,y in pairs)/len(pairs)
                                      for regime in REGIMES},
                    original_metrics=score(a),deep_metrics=score(b),
                    accuracy_difference_pp=100*(score(b)["modal_accuracy"]-score(a)["modal_accuracy"]),
                    mae_difference_c=score(b)["mae_c"]-score(a)["mae_c"],
                    conservative_interpretation_required=agreement<.8))
    return dict(**FLAGS,entries=entries,comparison_name="v0.3_partial_fidelity_vs_fixed_lead_reduced_fidelity",
                full_fidelity_available=False,
                limitations="Original v0.3 is partial fidelity. Model set, lead, freshness, 850hPa and archive variables differ jointly; this is total method drift, not an isolated model-count effect.")


def analyze(root, archive):
    deep=json.loads((root / "derived/checkpoints.json").read_text())
    cross=archive.parent / "madrid-historical-analysis-v1/derived/cross-check-records.json"
    original=[x["baseline"] for x in json.loads(cross.read_text()) if x["scenario"]=="main"]
    old_cross=[x for x in json.loads(cross.read_text()) if x["scenario"]=="main"]
    for r,item in zip(original,old_cross):
        r["evidence_class"]=ORIGINAL
        mapping={"current":"current_protected_forecast","anchor_disabled":"anchor_disabled",
                 "anchor_25pct":"anchor_25pct","anchor_50pct":"anchor_50pct","anchor_75pct":"anchor_75pct"}
        r["policies"]={name:item["policy_forecasts"][mapping.get(name,name)] for name,_ in SPECS[r["checkpoint_label"]]
                       if mapping.get(name,name) in item["policy_forecasts"]}
    cohorts={ORIGINAL:original,EVIDENCE:deep}
    regimes=regime_study(cohorts); policies=policy_study(cohorts)
    prices=json.loads((archive / "markets/checkpoint-price-series.json").read_text())
    market_file=root / "markets/checkpoint-price-series.json"
    if market_file.exists(): prices += json.loads(market_file.read_text())
    trading,no_trade,trades=trade_study(cohorts,prices)
    fidelity=fidelity_comparison(deep,original)
    monthly=[]
    for month in sorted({r["target_date"][:7] for r in deep}):
        g=[r for r in deep if r["target_date"].startswith(month)]
        monthly.append(dict(**meta(g,EVIDENCE),month=month,target_days=len({r["target_date"] for r in g}),
            accepted_actual_days=len({r["target_date"] for r in g if r.get("actual_complete")}),
            evaluable_checkpoints=sum(valid(r) for r in g),status_counts=dict(Counter(r["status"] for r in g)),
            model_checkpoint_coverage=dict(Counter(m for r in g for m in r["available_models"]))))
    extras=[r for r in deep if r["target_date"]<"2026-04-01"]
    priority=[r for r in deep if "2025-11-01"<=r["target_date"]<="2026-03-31"]
    earliest=min((r["target_date"] for r in extras if valid(r)),default=None)
    summary=dict(**FLAGS,version="deep_seasonal_replay_v1",earliest_seriously_usable_date=earliest,
        date_range=[str(START),str(END)],target_days=len(days()),checkpoints=len(deep),
        additional_target_days=len({r["target_date"] for r in extras}),additional_checkpoints=len(extras),
        accepted_actual_days=len({r["target_date"] for r in deep if r.get("actual_complete")}),
        evaluable_forecasts=sum(valid(r) for r in deep),priority_winter=dict(**meta(priority,EVIDENCE),
            target_days=len({r["target_date"] for r in priority}),accepted_actual_days=len({r["target_date"] for r in priority if r.get("actual_complete")})),
        evidence_classes=[ORIGINAL,EVIDENCE],available_model_cohort=list(MODELS),
        missing_models=sorted(set(AIRPORT["models"])-set(MODELS))+["meteoblue"],coverage_by_month=monthly,
        coverage_by_model={m:dict(checkpoints=sum(m in r["available_models"] for r in deep),
            date_range=meta([r for r in deep if m in r["available_models"]],EVIDENCE)["date_range"]) for m in MODELS},
        full_vs_deep=[e for e in fidelity["entries"] if e["dimension"]=="all"],
        regime_classifications=[e for e in regimes["season_interactions"] if e["evidence_class"]==EVIDENCE],
        policy_classifications=[e for e in policies["season_interactions"] if e["evidence_class"]==EVIDENCE],
        trading_classifications=[e for e in trading["season_interactions"] if e["evidence_class"]==EVIDENCE],
        market_coverage=json.loads((root / "markets/coverage.json").read_text()) if (root / "markets/coverage.json").exists() else {"status":"unavailable"},
        data_limits=["Fixed lead 48h D-1 / 24h D0 and Live; no verified historical per-run receipt times",
            "Causal bounds assume a conservative 6h10m publication lag; causality is conditional, not certified",
            "Private research freshness is a daily fixed-lead cohort, not production freshness",
            "No exact run age: initialization unknown; upper bounds and nominal forecast lead are recorded",
            "850hPa fixed-lead variables unsupported; not filled with reanalysis or retrospective hindcasts",
            "UKMO radiation and ARPEGE wind missing in archive periods; variable coverage stored",
            "Memory shadow enrichment unavailable; earlier Actuals and fixed-lead model bias calibration remain causal",
            "METAR-compatible complete daily maximum is not separately validated against settlement station feed",
            "Price series are not executable bid/ask quotes; gross hypothetical PnL excludes fees and slippage",
            "Multiple diagnostic comparisons, no clean untouched holdout, no certified OOS or live promotion"],
        production_forecast_engine_unchanged=True,production_champion_unchanged=True,
        new_regimes=False,new_challenger_families=False,production_parameter_changes=False)
    outputs={"seasonal-regime-tournament.json":regimes,"seasonal-policy-study.json":policies,
             "seasonal-trading-tournament.json":trading,"seasonal-no-trade-study.json":no_trade,
             "full-vs-deep-fidelity-comparison.json":fidelity,"deep-seasonal-replay-summary.json":summary}
    for name,obj in outputs.items(): atomic_json(root/name,clean(obj))
    atomic_json(root / "derived/trading-records.json",trades)
    report(root,summary,regimes,policies,trading,no_trade)


def report(root,summary,regimes,policies,trading,no_trade):
    lines=["# Deep Seasonal Historical Replay v1", "", 
        "Research only. Keine automatische Promotion. Production und Neon unverändert.","",
        f"Zeitraum: {START} bis {END}; {summary['target_days']} Tage, {summary['checkpoints']} Checkpoints. "
        f"Zusätzlich: {summary['additional_target_days']} Tage / {summary['additional_checkpoints']} Checkpoints. "
        f"Akzeptierte Actual-Tage: {summary['accepted_actual_days']}; evaluierbare Forecasts: {summary['evaluable_forecasts']}.","",
        "Die Deep-Kohorte verwendet kausal begrenzte historische Fixed-Lead-Prognosen. D−1: 48 h, D0/Live: 24 h. "
        "Das ist eine meteorologische Saisonstudie, keine exakte Rekonstruktion des heutigen damaligen Champions. "
        "Die Research-Adapter verwenden einen täglichen Fixed-Lead-Datenzyklus; die Produktionsfrischeprüfung bleibt unverändert. "
        "Ein einzelner Original-Run und seine tatsächliche Veröffentlichung sind nicht bekannt. "
        "available_at ist der späteste beitragende Run-Zeitpunkt als obere Schranke plus 6 h 10 min. "
        "Es wird ausschließlich available_at <= checkpoint_at ausgewählt; diese Kausalität bleibt von der Archivannahme abhängig.","",
        "## Coverage", "", "|Evidence class|Monat|Modelle|Tage|Actual-Tage|Forecasts N|", "|---|---|---|---:|---:|---:|"]
    for c in summary["coverage_by_month"]:
        lines.append(f"|{EVIDENCE}|{c['month']}|{', '.join(c['available_models'])}|{c['target_days']}|{c['accepted_actual_days']}|{c['evaluable_checkpoints']}|")
    lines += ["", "## Methodendrift im Überlappungszeitraum", "",
        "v0.3 ist ebenfalls Partial Fidelity; ‚Full vs Deep‘ bezeichnet keinen zertifizierten Full-Fidelity-Baseline. "
        "Modelle, Vorlauf, Frische, 850 hPa und Hilfsvariablen ändern sich gemeinsam. Die Differenz ist kein isolierter Modellanzahl-Effekt.","",
        "|Evidence classes|Checkpoint|Zeitraum|N|Modal agreement|Center delta MAE|Accuracy delta pp|MAE delta|",
        "|---|---|---|---:|---:|---:|---:|---:|"]
    for e in summary["full_vs_deep"]:
        lines.append(f"|{ORIGINAL} / {EVIDENCE}|{e['checkpoint']}|{'–'.join(e['date_range'])}|{e['n']}|{e['modal_agreement']:.1%}|{e['center_delta_mae_c']:.3f}|{e['accuracy_difference_pp']:+.2f}|{e['mae_difference_c']:+.3f}|")
    lines += ["", "Modelle je Vergleich und je Ergebniszelle stehen in den JSON-Dateien. "
        "Wenn Modal agreement unter 80% liegt, dürfen Deep-Effekte nur konservativ auf Production übertragen werden.","",
        "## Priorisierte Regimes", "", "|Evidence class|Checkpoint|Regime|Zeitraum|Warm N|Cold N|Warm lift pp|Cold lift pp|Status|",
        "|---|---|---|---|---:|---:|---:|---:|---|"]
    for e in summary["regime_classifications"]:
        if e["regime"] not in ("Stable / High Confidence","Rapid Heat Ramp","Temperature Plateau","Bucket Boundary"):continue
        w="unavailable" if e["warm_lift_pp"] is None else f"{e['warm_lift_pp']:+.2f}"
        c="unavailable" if e["cold_lift_pp"] is None else f"{e['cold_lift_pp']:+.2f}"
        lines.append(f"|{EVIDENCE}|{e['checkpoint']}|{e['regime']}|{'–'.join(e['date_range'])}|{e['warm_n']}|{e['cold_n']}|{w}|{c}|{e['classification']}|")
    lines += ["", "Alle Deep-Regime-Zellen verwenden das reduzierte Modellset " + ", ".join(MODELS) + ". "
        "Konkrete Modellsets und Sample-Klassen stehen je JSON-Zelle; fehlende oder kleine Gruppen bleiben explorativ.","",
        "## Anchor und Late Dry Mixing", "", "|Evidence class|Checkpoint|Policy|Saison|Zeitraum|N|Baseline accuracy|Policy accuracy|Delta pp|MAE delta|",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|"]
    for e in policies["entries"]:
        if e["evidence_class"]!=EVIDENCE or e["dimension"]!="season":continue
        c=e["comparison"]
        if not e["n"]:continue
        lines.append(f"|{EVIDENCE}|{e['checkpoint']}|{e['policy_id']}|{e['period']}|{'–'.join(e['date_range'])}|{e['n']}|{c['baseline']['modal_accuracy']:.1%}|{c['policy']['modal_accuracy']:.1%}|{c.get('accuracy_delta_pp',0):+.2f}|{c.get('mae_delta_c',0):+.3f}|")
    lines += ["", "## Trading", "",
        "price_based_hypothetical_pnl; archivierte Polymarket-Preiszeitreihen, keine historischen ausführbaren Quotes. "
        "Einsatz 1 pro Checkpoint, hypothetische Startbankroll 1000, keine Gebühren, Slippage oder Wiederanlage. "
        "Checkpoint-Portfolios und Fidelity-Klassen werden getrennt ausgewertet.","",
        "|Evidence class|Checkpoint|Strategie|Saison|Zeitraum|N|ROI|P&L|Max DD|Loss streak|",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|"]
    for e in trading["entries"]:
        if e["evidence_class"]!=EVIDENCE or e["dimension"]!="season" or e["freshness_minutes"]!=30:continue
        if not e["n"]:continue
        s=e["metrics"]
        lines.append(f"|{EVIDENCE}|{e['checkpoint']}|{e['strategy']}|{e['period']}|{'–'.join(e['date_range'])}|{e['n']}|{s['roi']:.1%}|{s['hypothetical_pnl']:+.2f}|{s['max_drawdown']:.2f}|{s['longest_losing_streak']}|")
    lines += ["", "Die JSON-Dateien enthalten ≤10/30/60 Minuten und >60 Minuten als reine Sensitivität, "
        "alle Monatswerte, Regime-Portfolios und NO-TRADE-Effekte einschließlich kleiner/unverfügbarer Gruppen. "
        "Unverfügbare Winterpreise erlauben keine Aussage über saisonale Trading-Robustheit.","", "## Datenlimits", ""]
    lines += ["- "+x for x in summary["data_limits"]]
    lines += ["", "Keine neuen Regimes, keine neuen Challenger-Familien, keine Production-Änderung, "
        "kein Production-Neon-Backfill. research_only=true; automatic_promotion=false.", ""]
    # Every human-readable table carries the model cohort as a visible column.
    rendered=[];append_models=False
    for line in lines:
        if line.startswith("|Evidence"):
            append_models="Modelle" not in line
            rendered.append(line+"Modelle|" if append_models else line)
        elif line.startswith("|") and append_models:
            models="---" if line.startswith("|---") else ", ".join(MODELS)
            rendered.append(line+models+"|")
        else:
            append_models=False;rendered.append(line)
    (root / "deep-seasonal-replay-report.md").write_text("\n".join(rendered))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--archive",type=Path,required=True)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--mode",choices=("acquire","replay","analyze","all"),default="all")
    parser.add_argument("--workers",type=int,default=6)
    args=parser.parse_args()
    root=args.root.resolve(); root.mkdir(parents=True,exist_ok=True)
    if root in (args.archive.resolve(),args.source.resolve(),REPO):
        raise ArchiveError("Output must be isolated")
    if args.mode in ("acquire","all"): acquire(root,args.archive,args.source)
    if args.mode in ("replay","analyze","all"): prohibit_network()
    if args.mode in ("replay","all"): replay(root,args.workers)
    if args.mode in ("analyze","all"): analyze(root,args.archive)


if __name__=="__main__": main()
