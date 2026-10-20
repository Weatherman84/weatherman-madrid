"""Research-only archive enrichment and isolated full-engine policy experiments."""

from __future__ import annotations

import ast
import csv
import inspect
import io
import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd

from . import nowcast as engine
from .historical_archive import ArchiveError, SINGLE_RUNS_URL, digest, group_time, utc

OGIMET_URL = "https://www.ogimet.com/cgi-bin/gettafor"
PREVIOUS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ENRICHMENT_VERSION = "historical_enrichment_v0.3.0"


def decode_conditions(text, inherited):
    result = dict(inherited)
    wind = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\b", text)
    if wind:
        scale = 1 if wind[4] == "KT" else 1.943844
        result.update(
            wind_direction=None if wind[1] == "VRB" else float(wind[1]),
            wind_speed_kt=float(wind[2]) * scale,
            wind_gust_kt=float(wind[3]) * scale if wind[3] else None,
        )
    clouds = re.findall(r"\b(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?\b", text)
    if clouds:
        result["clouds"] = [
            {"cover": c, "base": int(h) * 100, "type": typ or None} for c, h, typ in clouds
        ]
    if re.search(r"\b(CAVOK|NSC|SKC|NCD)\b", text):
        result["clouds"] = [{"cover": "SKC", "base": None, "type": None}]
        result["weather"] = ""
    weather = re.findall(
        r"(?<!\w)[+-]?(?:VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|PL|GR|GS|BR|FG|FU|VA|DU|SA|HZ|SQ|FC|SS|DS)+(?!\w)",
        text,
    )
    if weather:
        result["weather"] = " ".join(weather)
    if re.search(r"\bNSW\b", text):
        result["weather"] = ""
    return result


def decode_taf_periods(raw, issue, start, stop):
    """Decode prevailing/FM/BECMG and temporary/probability groups, preserving raw text."""
    validity = re.search(r"\b\d{4}/\d{4}\b", raw)
    if not validity:
        return []
    body = raw[validity.end() :].rstrip("=")
    marker = re.compile(
        r"\b(?:(PROB(?:30|40))(?:\s+(TEMPO))?\s+(\d{4}/\d{4})|(BECMG|TEMPO)\s+(\d{4}/\d{4})|FM(\d{6}))\b"
    )
    matches = list(marker.finditer(body))
    prevailing = decode_conditions(body[: matches[0].start()] if matches else body, {})
    prevailing_index = 0
    periods = [
        {
            **prevailing,
            "time_from": utc(start).isoformat(),
            "time_to": utc(stop).isoformat(),
            "change": None,
            "probability": None,
        }
    ]
    for i, m in enumerate(matches):
        text = body[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(body)]
        if m[6]:
            token = m[6]
            begin = group_time(issue, int(token[:2]), int(token[2:4])) + timedelta(
                minutes=int(token[4:])
            )
            end = stop
            change = "FM"
            prob = None
        else:
            token = m[3] or m[5]
            begin = group_time(issue, int(token[:2]), int(token[2:4]))
            end = group_time(issue, int(token[5:7]), int(token[7:9]))
            change = (m[2] or m[1]) if m[1] else m[4]
            prob = int(m[1][4:]) if m[1] else None
        if not start <= begin < end <= stop:
            raise ArchiveError("TAF change interval outside validity")
        conditions = decode_conditions(text, prevailing)
        if change in {"FM", "BECMG"}:
            # BECMG conditions may occur during transition and prevail afterwards.
            periods[prevailing_index]["time_to"] = utc(
                end if change == "BECMG" else begin
            ).isoformat()
            prevailing = conditions
            prevailing_index = len(periods)
            periods.append(
                {
                    **conditions,
                    "time_from": utc(begin).isoformat(),
                    "time_to": utc(stop).isoformat(),
                    "time_bec": utc(end).isoformat() if change == "BECMG" else None,
                    "change": change,
                    "probability": prob,
                }
            )
        else:
            periods.append(
                {
                    **conditions,
                    "time_from": utc(begin).isoformat(),
                    "time_to": utc(end).isoformat(),
                    "change": change,
                    "probability": prob,
                }
            )
    return periods


def ogimet_rows(data, metadata):
    rows = []
    for fields in csv.reader(io.StringIO(data.decode("utf-8"))):
        if not fields:
            continue
        if len(fields) < 7 or fields[0] != "LEMD":
            raise ArchiveError("Malformed OGIMET row")
        archive_stamp = datetime(*map(int, fields[1:6]), tzinfo=timezone.utc)
        raw = ",".join(fields[6:]).strip()
        issue_token = re.search(r"\bLEMD\s+(\d{2})(\d{2})(\d{2})Z\b", raw)
        validity = re.search(r"\b(\d{2})(\d{2})/(\d{2})(\d{2})\b", raw)
        if not issue_token or not validity:
            continue
        issue = group_time(archive_stamp, int(issue_token[1]), int(issue_token[2])) + timedelta(
            minutes=int(issue_token[3])
        )
        start = group_time(issue, int(validity[1]), int(validity[2]))
        stop = group_time(issue, int(validity[3]), int(validity[4]))
        tx = re.search(r"\bTX(M?\d{2})/(\d{2})(\d{2})Z\b", raw)
        try:
            periods = decode_taf_periods(raw, issue, start, stop)
            decode_error = None
        except ArchiveError as exc:
            periods = []
            decode_error = str(exc)
        rows.append(
            {
                "airport": "LEMD",
                "issue_time": issue,
                "available_at": issue + timedelta(minutes=5),
                "valid_from": start,
                "valid_to": stop,
                "raw_taf": raw,
                "is_amended": bool(re.search(r"\bAMD\b", raw)),
                "is_corrected": bool(re.search(r"\bCOR\b", raw)),
                "max_temp_c": (-float(tx[1][1:]) if tx[1].startswith("M") else float(tx[1]))
                if tx
                else None,
                "max_temp_at": group_time(issue, int(tx[2]), int(tx[3])) if tx else None,
                "periods_json": json.dumps(periods) if periods else None,
                "decoded_periods_available": bool(periods),
                "decode_error": decode_error,
                "first_seen_at": None,
                "collected_at": metadata["fetched_at"],
                "fetched_at": metadata["fetched_at"],
                "backfilled": True,
                "source": "ogimet",
                "availability_basis": "estimated_issue_plus_5_minutes",
                "archive_row_timestamp": archive_stamp,
                "content_hash": digest(raw.encode()),
                "source_sha256": metadata["sha256"],
            }
        )
    return pd.DataFrame(rows)


def fetch_ogimet(cache, start, end):
    data, meta = cache.get(
        OGIMET_URL,
        {"icao": "LEMD", "begin": f"{start:%Y%m%d}0000", "end": f"{end:%Y%m%d}0000"},
        kind="ogimet_taf",
    )
    return ogimet_rows(data, meta)


def fetch_pressure(cache, airport, run, models):
    data, meta = cache.get(
        SINGLE_RUNS_URL,
        {
            "latitude": airport["latitude"],
            "longitude": airport["longitude"],
            "models": ",".join(sorted(models)),
            "run": f"{run:%Y-%m-%dT%H:%M}",
            "hourly": "temperature_850hPa",
            "timezone": "UTC",
            "forecast_days": 3,
        },
        kind="json",
    )
    hourly = json.loads(data)["hourly"]
    frames = []
    for model in sorted(models):
        key = "temperature_850hPa" + ("_" + model if len(models) > 1 else "")
        frame = pd.DataFrame(
            {
                "valid_at": pd.to_datetime(hourly["time"], utc=True),
                "model": model,
                "model_run_at": run,
                "temp_850hpa_c": hourly.get(key, [None] * len(hourly["time"])),
                "pressure_source_sha256": meta["sha256"],
                "pressure_fetched_at": meta["fetched_at"],
            }
        )
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def policy_parameters(checkpoint, name):
    if checkpoint.startswith("D0"):
        values = {
            "current_anchor_100pct": 1.0,
            "anchor_75pct": 0.75,
            "anchor_50pct": 0.5,
            "anchor_25pct": 0.25,
            "anchor_disabled": 0.0,
            "anchor_capped_0_10c": None,
        }
        return {"temperature_anchor": values[name]}
    if checkpoint.startswith("Late"):
        parts = name.split("_")
        return {
            "clear_sky_override": int(parts[1]) / 100,
            "taf": int(parts[3]) / 100,
            "late_dry_mixing": int(parts[5]) / 100,
        }
    return {}


@lru_cache(maxsize=32)
def policy_engine(parameters_json):
    """Compile an isolated copy of the protected function with four audited input hooks.

    No module monkeypatch or production file change. Identity parameters must match
    the native engine; corrections are changed BEFORE guards, consensus and conditioning.
    """
    parameters = json.loads(parameters_json)
    tree = ast.parse(inspect.getsource(engine.build_live_nowcast))
    function = tree.body[0]
    inserts = 0
    body = []
    for statement in function.body:
        body.append(statement)
        if isinstance(statement, ast.Assign):
            assigned = {
                node.id
                for target in statement.targets
                for node in ast.walk(target)
                if isinstance(node, ast.Name)
            }
            for factor, signal in (
                ("late_dry_mixing", "late_dry_mixing_signal"),
                ("clear_sky_override", "clear_sky_signal"),
            ):
                if (
                    factor in assigned
                    and signal in assigned
                    and isinstance(statement.value, ast.Call)
                ):
                    body.extend(
                        ast.parse(
                            f"{factor} *= research_parameters.get('{factor}', 1.0)\nif {factor} == 0:\n    {signal} = None"
                        ).body
                    )
                    inserts += 1
        if (
            isinstance(statement, ast.Assign)
            and isinstance(statement.value, ast.Dict)
            and any(isinstance(t, ast.Name) and t.id == "contributions" for t in statement.targets)
        ):
            hook = ast.parse(
                "for factor, multiplier in research_parameters.items():\n    if factor in contributions and factor not in {'late_dry_mixing', 'clear_sky_override'}:\n        contributions[factor] = max(-0.10, min(0.10, contributions[factor])) if multiplier is None else contributions[factor] * multiplier\n"
            ).body
            body.extend(hook)
            inserts += 1
        if isinstance(statement, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "taf_center_adjustment" for t in statement.targets
        ):
            body.extend(
                ast.parse("taf_center_adjustment *= research_parameters.get('taf', 1.0)").body
            )
            inserts += 1
    if inserts != 4:
        raise ArchiveError("Protected engine hook locations changed")
    function.body = body
    ast.fix_missing_locations(tree)
    namespace = {**vars(engine), "research_parameters": parameters}
    exec(compile(tree, "<isolated_historical_policy_engine>", "exec"), namespace)
    return namespace["build_live_nowcast"]


def shift_availability(frame, extra_minutes):
    result = frame.copy()
    if not result.empty:
        estimated = ~result.availability_basis.eq("verified_historical_availability")
        result["available_at"] = pd.to_datetime(result.available_at, utc=True)
        result.loc[estimated, "available_at"] = result.loc[
            estimated, "available_at"
        ] + pd.Timedelta(minutes=extra_minutes)
    return result
