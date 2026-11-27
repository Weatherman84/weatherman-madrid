"""Versioned research source: one daily/hourly payload per model and local day.

This is a receipt-time snapshot of the provider's current product, not proof of
an isolated meteorological run. Metadata must remain stable around the request.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import json

import httpx

from .madrid_late_shadow_model import MODELS, bounds, cutoff, instant, digest
from .shadow_capture_health import CaptureRejected

SOURCE_CLASS = 'live_open_meteo_coherent_day_v2'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
METADATA_MODELS = {'ecmwf_ifs025': 'ecmwf_ifs025', 'gfs_global': 'ncep_gfs025',
                   'icon_global': 'dwd_icon', 'icon_eu': 'dwd_icon_eu',
                   'meteofrance_arpege_europe': 'meteofrance_arpege_europe',
                   'ukmo_global_deterministic_10km': 'ukmo_global_deterministic_10km'}


def request_params(day):
    return {'latitude': 40.466, 'longitude': -3.555, 'daily': 'temperature_2m_max',
            'hourly': 'temperature_2m', 'timezone': 'Europe/Madrid', 'timeformat': 'unixtime',
            'start_date': str(day), 'end_date': str(day)}


SOURCE_CONTRACT = {'source_class': SOURCE_CLASS, 'request': request_params('TARGET_DATE'),
                   'models': list(MODELS), 'metadata': 'unchanged run and availability around response',
                   'coverage': 'all UTC instants of the Europe/Madrid local day; no duplicates',
                   'daily_maximum_tolerance_c': 1e-8, 'maximum_run_age_hours': 48,
                   'no_older_run_substitution': True, 'receipt': 'all payloads before cutoff'}
CONTRACT_SHA = digest(SOURCE_CONTRACT)


def parse_model(day, model, payload, before, after, fetched_at):
    if not all(isinstance(value, dict) for value in (payload, before, after)):
        raise CaptureRejected('invalid_source_format', 'Invalid model response format', model=model)
    start, end = bounds(day)
    cut, fetched = cutoff(day), instant(fetched_at)
    if not start <= fetched < cut:
        raise CaptureRejected('outside_window', 'Coherent payload arrived after cutoff', model=model)
    keys = ('last_run_initialisation_time', 'last_run_availability_time')
    if any(before.get(k) != after.get(k) or not isinstance(before.get(k), (float, int)) for k in keys):
        raise CaptureRejected('model_updated_during_fetch', 'Model metadata changed or is missing', model=model)
    run, available = (datetime.fromtimestamp(before[k], timezone.utc) for k in keys)
    if not run <= available <= fetched or fetched - run > timedelta(hours=48):
        raise CaptureRejected('invalid_model_metadata', 'Model metadata invalid/stale', model=model)
    times = payload.get('hourly', {}).get('time', [])
    temps = payload.get('hourly', {}).get('temperature_2m', [])
    expected = [int((start + timedelta(hours=i)).timestamp())
                for i in range(int((end - start).total_seconds() // 3600))]
    daily = payload.get('daily', {}).get('temperature_2m_max', [])
    if times != expected or len(temps) != len(expected) or any(
            not isinstance(v, (int, float)) or not math.isfinite(v) for v in temps):
        raise CaptureRejected('incomplete_local_day', 'Missing/duplicate local-day hours', model=model,
                              expected_hours=len(expected), received_hours=len(times))
    if len(daily) != 1 or not isinstance(daily[0], (int, float)) or not math.isfinite(daily[0]):
        raise CaptureRejected('invalid_daily_maximum', 'Missing/invalid daily maximum', model=model)
    # A daily/hourly mismatch remains a documented reference fallback in the
    # unchanged feature adapter; never silently alter either input maximum.
    forecast = {'id': MODELS.index(model), 'model': model, 'run_at': run.isoformat(),
                'model_run_at': run.isoformat(), 'available_at': available.isoformat(),
                'fetched_at': fetched.isoformat(), 'max_temp_c': daily[0],
                'provenance_status': 'coherent current-product receipt; stable surrounding metadata'}
    hourly = [{'model': model, 'run_at': run.isoformat(), 'valid_at': datetime.fromtimestamp(t, timezone.utc).isoformat(),
               'temp_c': v} for t, v in zip(times, temps)]
    return forecast, hourly


def fetch_models(day, *, clock=lambda: datetime.now(timezone.utc), client_factory=httpx.Client):
    """Bound concurrency and fail closed; never borrow an older run or replay."""
    def fetch(model):
        if clock() >= cutoff(day) - timedelta(seconds=30):
            raise CaptureRejected('insufficient_capture_time', 'No time for a bounded source fetch', model=model)
        metadata_url = f'https://api.open-meteo.com/data/{METADATA_MODELS[model]}/static/meta.json'
        params = {**request_params(day), 'models': model}
        with client_factory(timeout=8) as client:
            def get(url, params=None):
                response = client.get(url, params=params)
                response.raise_for_status()
                if len(response.content) > 1_000_000:
                    raise CaptureRejected('oversized_source', 'Oversized model response', model=model)
                return response
            before = get(metadata_url)
            weather = get(FORECAST_URL, params)
            after = get(metadata_url)
            received = clock()
        forecast, hourly = parse_model(day, model, weather.json(), before.json(), after.json(), received)
        return forecast, hourly, {'model': model, 'request': params, 'received_at': received.isoformat(),
                                  'raw_forecast_text': weather.text, 'metadata_before_text': before.text,
                                  'metadata_after_text': after.text}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(fetch, MODELS))
    except CaptureRejected:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise CaptureRejected('model_source_unavailable', 'Coherent model request failed',
                              error_class=type(exc).__name__) from None
    return {'forecasts': [r[0] for r in results], 'hourly': [p for r in results for p in r[1]],
            'archive': {'source_class': SOURCE_CLASS, 'target_date': str(day),
                        'source_contract_sha256': CONTRACT_SHA,
                        'classification': 'prospective_input_bundle_not_forecast',
                        'models': [r[2] for r in results]}}


def validate_bundle(row):
    bundle = row.get('coherent_input_bundle', {})
    if (bundle.get('source_class') != SOURCE_CLASS or bundle.get('target_date') != row['target_date']
            or bundle.get('source_contract_sha256') != CONTRACT_SHA
            or {r['model'] for r in bundle.get('models', [])} != set(MODELS)
            or len(bundle['models']) != len(MODELS)):
        raise ValueError('Invalid coherent input contract')
    snapshots = row['source_snapshot']
    for raw in bundle['models']:
        model = raw['model']
        if raw['request'] != {**request_params(row['target_date']), 'models': model}:
            raise ValueError('Changed coherent source request')
        forecast, hourly = parse_model(row['target_date'], model, json.loads(raw['raw_forecast_text']),
                                      json.loads(raw['metadata_before_text']), json.loads(raw['metadata_after_text']),
                                      raw['received_at'])
        if (forecast not in snapshots['forecasts']
                or hourly != [p for p in snapshots['hourly'] if p['model'] == model]
                or row['models'][model] != forecast['max_temp_c']):
            raise ValueError('Coherent bundle does not reproduce frozen source rows')
    return bundle
