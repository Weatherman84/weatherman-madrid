"""Wrap the protected collector with pre-cutoff source preparation and coverage.

The forecast engine and collector implementation stay byte-identical. Preparation
only uses the existing forecast-source writer; it creates no decisions/actuals.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from weatherman.madrid_late_shadow_model import ZONE


def preparation_due(now):
    local = now.astimezone(ZONE)
    return local.hour in {8, 11, 15, 19} and 20 <= local.minute <= 50


def refresh_sources(now):
    from sqlalchemy import select
    from weatherman.catalog import trading_airports
    from weatherman.db import Forecast, Session
    from weatherman.service import _store_current_provider_forecasts

    with Session() as session:
        result = _store_current_provider_forecasts(session, airport_code='LEMD',
                    airport=trading_airports()['LEMD'], as_of=now, force_refresh=True)
        session.commit()
        # Verify committed current-day source rows, not API success counters.
        selected = list(session.scalars(select(Forecast).where(
            Forecast.airport == 'LEMD', Forecast.target_date == now.astimezone(ZONE).date(),
            Forecast.fetched_at >= now)))
        result['committed_receipts'] = [{k: getattr(r, k).isoformat() if isinstance(getattr(r, k), datetime)
                                        else getattr(r, k)
                                        for k in ('model', 'run_at', 'model_run_at', 'fetched_at', 'available_at', 'max_temp_c')}
                                       for r in selected]
    return result


def prepare(now, *, refresh=refresh_sources, clock=lambda: datetime.now(timezone.utc)):
    if not preparation_due(now):
        return {'status': 'not_due', 'classification': 'source_preparation_not_forecast'}
    cut = now.astimezone(ZONE).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    attempts = []
    for _ in range(2):
        actual = clock()
        if actual >= cut - timedelta(minutes=5):
            break
        try:
            result = refresh(actual)
        except Exception as exc:
            attempts.append({"error_class": type(exc).__name__})
            continue
        receipts = [r for r in result.get('committed_receipts', [])
                    if datetime.fromisoformat(r['fetched_at']) < cut]
        attempts.append({'forecasts': result.get('forecasts', 0),
                         'hourly_forecasts': result.get('hourly_forecasts', 0),
                         'committed_receipts': receipts})
        if len({r['model'] for r in receipts}) >= 2 and result.get('hourly_forecasts', 0) > 0:
            return {'status': 'prepared', 'classification': 'source_preparation_not_forecast',
                    'cutoff': cut.isoformat(), 'attempts': attempts}
    return {'status': 'source_preparation_failed', 'classification': 'source_preparation_not_forecast',
            'cutoff': cut.isoformat(), 'attempts': attempts}


def verify_current_checkpoint(now):
    """Verify an actual stored checkpoint after grace; no later reconstruction."""
    from sqlalchemy import select
    from weatherman.db import ForecastSnapshot, Session
    local = now.astimezone(ZONE)
    if local.hour not in {9, 12, 16, 20}:
        return {'status': 'not_due'}
    day = local.date() + timedelta(days=1) if local.hour == 20 else local.date()
    cut = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    with Session() as session:
        rows = list(session.scalars(select(ForecastSnapshot).where(
            ForecastSnapshot.airport == 'LEMD', ForecastSnapshot.target_date == day,
            ForecastSnapshot.checkpoint_at == cut)))
        valid = [r for r in rows if r.checkpoint_reconstructed is False
                 and r.checkpoint_status == 'scheduled-causal']
        return {'status': 'recorded' if len(valid) == 1 else 'checkpoint_missing_or_ambiguous',
                'target_date': day.isoformat(), 'checkpoint_at': cut.isoformat(),
                'persisted_forecast_ids': [r.id for r in valid]}


def main():
    from weatherman.collector import run_collector
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='auto', choices=('auto', 'aviation', 'fixed'))
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    # Bound concurrency and timeouts without editing protected source defaults.
    preparation = prepare(started)
    result = run_collector(['LEMD'], collection_mode=args.mode)
    coverage = verify_current_checkpoint(datetime.now(timezone.utc))
    health = {'source_preparation': preparation, 'checkpoint_recording': coverage,
              'classification': 'operational_coverage_not_forecast_skill'}
    Path('madrid-checkpoint-health.json').write_text(json.dumps(health, allow_nan=False, indent=2))
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write('Madrid checkpoint recording: ' + coverage['status'] + '\n')
    print(result)
    print(json.dumps(health))
    if coverage['status'] == 'checkpoint_missing_or_ambiguous':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
