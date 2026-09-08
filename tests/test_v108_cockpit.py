from datetime import date, datetime, timedelta, timezone
import json

import pandas as pd
import pytest
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, create_engine, event
from sqlalchemy.orm import sessionmaker

from weatherman import cockpit_data as cockpit
from weatherman.aemet_live import curve_rows, filter_metar_day, observation_freshness
from weatherman.aemet_metar_shadow import build_shadow_diagnostics, comparison_bucket
from weatherman.catalog import trading_airports
from weatherman.db import (
    Base, DailyActual, Forecast, ForecastSnapshot, ForecastVariantSnapshot, HourlyForecast,
    MarketSnapshot, Observation, TafReport,
)
from weatherman.history import read_archive_live
from weatherman.service import build_current_live_nowcast


@pytest.mark.parametrize('day,hours', [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25),
                                      (date(2026, 9, 6), 24)])
def test_aemet_local_day_excludes_previous_high_and_next_midnight(day, hours):
    start, end = cockpit.local_day_bounds(day)
    assert (end - start).total_seconds() == hours * 3600
    records = [{'observed_at': stamp.isoformat(), 'temp_c': temp} for stamp, temp in [
        (start - timedelta(seconds=1), 39), (start, 35),
        (start + timedelta(hours=12), 37), (end - timedelta(seconds=1), 36), (end, 40),
    ]]
    payload = {
        'local_date': day.isoformat(),
        'observations': [{'observed_at': (start + timedelta(hours=i)).isoformat(),
                          'temperature_c': temp} for i, temp in [(10, 37.2), (11, 37.6), (12, 37.5)]],
        'physical_tmax': {'value_c': 37.6, 'observed_at': (start + timedelta(hours=11)).isoformat()},
    }
    assert len(filter_metar_day(records, day)) == 3
    result = build_shadow_diagnostics(payload, records)
    assert result['ground_truth']['stored_metar_max']['value_c'] == 37
    assert result['ground_truth']['daily_max_series_gap_c'] == 0.6
    assert result['metar_bucket_persistence']['stored_metar_max_c'] == 37
    pairs = result['ground_truth']['time_aligned_series_comparisons']
    assert pairs[0]['aemet_comparison_bucket_c'] == 38
    assert pairs[0]['metar_temperature_bucket_c'] == 37
    assert pairs[0]['bucket_match'] == 'DIFF'
    assert max(row['temperature_c'] for row in curve_rows(payload, records)
               if row['series'] == 'LEMD METAR (integer)') == 37


@pytest.mark.parametrize('value,expected', [(36.49, 36), (36.5, 37), (37.5, 38), (-1.5, -1)])
def test_comparison_rounding_is_explicit(value, expected):
    assert comparison_bucket(value) == expected


@pytest.mark.parametrize('minutes,status', [(75, 'current'), (75.01, 'delayed'), (120, 'delayed'),
                                          (120.01, 'stale')])
def test_observation_freshness_is_recomputed_between_fetches(minutes, status):
    now = datetime(2026, 9, 6, 18, tzinfo=timezone.utc)
    assert observation_freshness(now - timedelta(minutes=minutes), now)[0] == status


def _insert(session, table, **values):
    # Complete synthetic ORM rows, including deliberately large unused JSON.
    for col in table.__table__.columns:
        if col.name in values or col.primary_key or col.nullable or col.default is not None:
            continue
        if isinstance(col.type, DateTime):
            values[col.name] = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
        elif isinstance(col.type, Date):
            values[col.name] = date(2026, 9, 6)
        elif isinstance(col.type, Boolean):
            values[col.name] = False
        elif isinstance(col.type, (Float, Integer)):
            values[col.name] = 0
        else:
            values[col.name] = ''
    session.add(table(**values))


@pytest.fixture
def database(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cockpit, 'Session', factory)
    for func in (cockpit.load_madrid_data, cockpit.load_madrid_history, cockpit.load_cockpit_details):
        func.clear()
    target = date(2026, 9, 6)
    now = datetime(2026, 9, 6, 12, 10, tzinfo=timezone.utc)
    with factory.begin() as session:
        for offset in range(0, 9):
            day = target - timedelta(days=offset)
            at = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=12)
            for model, value in [('icon_eu', 37.4), ('ecmwf', 36.7), ('arpege', 37.0)]:
                for horizon, run, temp in [('D-1', at - timedelta(hours=18), value),
                                           ('Live', at, value + 0.3)]:
                    _insert(session, Forecast, airport='LEMD', model=model, target_date=day,
                            run_at=run, fetched_at=run, available_at=run, max_temp_c=temp,
                            source='open-meteo', horizon=horizon)
            _insert(session, DailyActual, airport='LEMD', target_date=day,
                    max_temp_c=37, source='stored-metar-station')
            _insert(session, ForecastSnapshot, airport='LEMD', target_date=day,
                    captured_at=at, final_forecast_c=37.1, hours_to_peak=3,
                    final_spread_c=0.7, taf_adjustment_c=0, temp_anchor_adjustment_c=0.2,
                    day_phase='live', checkpoint_label='First Live @12:00',
                    checkpoint_status='scheduled-causal', features_json=json.dumps({
                        'hours_to_peak': 3, 'effective_temperature_residual_c': 0.6,
                        'temperature_anchor_streak': 3, 'temperature_anchor_gain': 0.5,
                        'cloud_cover': 0, 'wind_kph': 10,
                    }), source_provenance_json=json.dumps({'unused': 'X' * 20000}))
            for variant, factor in [('Champion', None), ('Analog', 'regime_memory_analog'),
                                    ('Anchor', 'anchor_transfer')]:
                _insert(session, ForecastVariantSnapshot, airport='LEMD', target_date=day,
                        captured_at=at, timing='First Live', variant=variant, factor=factor,
                        forecast_c=37.1, probabilities_json='{"36":0.1,"37":0.8,"38":0.1}')
        for hour in range(8, 19):
            at = now.replace(hour=hour, minute=0)
            for model in ['icon_eu', 'ecmwf', 'arpege']:
                _insert(session, HourlyForecast, airport='LEMD', model=model,
                        run_at=now.replace(hour=8, minute=0), valid_at=at,
                        temp_c=37 - abs(16-hour) * 0.4, cloud_cover=0, wind_kph=10,
                        wind_direction=220, radiation_wm2=600)
            if hour <= 12:
                _insert(session, Observation, airport='LEMD', observed_at=at,
                        temp_c=35 + (hour - 8) * 0.4, cloud_cover=0,
                        dewpoint_c=7, wind_kph=10, wind_direction=220, raw='CAVOK')
    yield engine, target, now
    engine.dispose()


def test_projected_reads_preserve_forecast_and_oos_evidence(database):
    engine, target, now = database
    with engine.connect() as bind:
        full = {name: read_archive_live(model, bind) for name, model in {
            'forecasts': Forecast, 'actuals': DailyActual, 'observations': Observation,
            'hourly': HourlyForecast, 'markets': MarketSnapshot, 'tafs': TafReport,
            'snapshots': ForecastSnapshot, 'variants': ForecastVariantSnapshot,
        }.items()}
    current = cockpit.load_madrid_data(target, 'LEMD', 'Europe/Madrid')
    history = cockpit.load_madrid_history(target, 'LEMD')
    reduced = cockpit.combine_cockpit_data(current, history)
    assert 'source_provenance_json' not in reduced['snapshots']
    assert len(reduced['snapshots']) == len(full['snapshots'])
    assert reduced['snapshots'].memory_usage(deep=True).sum() < full['snapshots'].memory_usage(deep=True).sum() / 5
    assert len(reduced['forecasts']) < len(full['forecasts'])
    assert reduced['variants'].probabilities_json.notna().all()
    baseline = build_current_live_nowcast(airport=trading_airports()['LEMD'], target=target,
                                         captured_at=now, **full)
    result = build_current_live_nowcast(airport=trading_airports()['LEMD'], target=target,
                                       captured_at=now, **{key: reduced[key] for key in full})
    assert result is not None and baseline is not None
    for name in ['raw_model_mean', 'corrected', 'model_weights', 'adjustment_contributions',
                 'final_forecast_mean', 'final_forecast_spread', 'probabilities', 'day_status',
                 'taf_adjustment_c', 'challenger_variants', 'regime_memory']:
        assert getattr(result, name) == getattr(baseline, name), name
    pd.testing.assert_frame_equal(cockpit.checkpoint_reliability(reduced['snapshots'], reduced['actuals']),
                                  cockpit.checkpoint_reliability(full['snapshots'], full['actuals']))


def test_cache_reuses_reads_and_refresh_invalidates_only_target(database):
    engine, target, _ = database
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda conn, cursor, statement, *args:
                 statements.append(statement))
    cockpit.load_madrid_data(target, 'LEMD', 'Europe/Madrid')
    cockpit.load_madrid_history(target, 'LEMD')
    count = len(statements)
    cockpit.load_madrid_data(target, 'LEMD', 'Europe/Madrid')
    cockpit.load_madrid_history(target, 'LEMD')
    assert len(statements) == count
    assert not any('provider_calls' in item for item in statements)
    assert not any('source_provenance_json' in item for item in statements)
    cockpit.invalidate_cockpit_cache(target, 'LEMD', 'Europe/Madrid')
    cockpit.load_madrid_data(target, 'LEMD', 'Europe/Madrid')
    assert len(statements) > count


def test_export_marks_missing_archive_and_rejects_wrong_day(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from weatherman import daily_analysis_export as export

    monkeypatch.setattr(export, 'settings', SimpleNamespace(aemet_public_base_url='https://test.example'))
    def fetch(base, path):
        if 'archive' in path:
            response = httpx.Response(404, request=httpx.Request('GET', base + '/' + path))
            response.raise_for_status()
        return {'local_date': '2026-09-05'}
    monkeypatch.setattr(export, 'fetch_public_aemet_json', fetch)
    result = export._aemet_physical_payload(date(2026, 9, 5), date(2026, 9, 6))
    assert result['days'][0]['availability_reason'] == 'archive_missing'
    assert result['days'][1]['status'] == 'unavailable'
    assert 'does not match' in result['days'][1]['reason']


def test_streamlit_cockpit_renders_aemet_after_buckets_without_extra_sql(database, monkeypatch):
    from dataclasses import replace
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import weatherman.aemet_live as aemet
    import weatherman.db as db
    import weatherman.service as service
    import weatherman.settings as settings_module

    _, target, now = database
    current = cockpit.load_madrid_data(target, 'LEMD', 'Europe/Madrid')
    history = cockpit.load_madrid_history(target, 'LEMD')
    data = cockpit.combine_cockpit_data(current, history)
    nowcast = build_current_live_nowcast(airport=trading_airports()['LEMD'], target=target,
        captured_at=now, **{name: data[name] for name in [
            'forecasts', 'actuals', 'observations', 'hourly', 'markets', 'tafs', 'snapshots', 'variants']})
    monkeypatch.setattr(cockpit, 'load_madrid_data', lambda *args: current)
    monkeypatch.setattr(cockpit, 'load_madrid_history', lambda *args: history)
    monkeypatch.setattr(db, 'init_db', lambda: None)
    monkeypatch.setattr(service, 'build_current_live_nowcast', lambda **kwargs: nowcast)
    monkeypatch.setattr(settings_module, 'settings', replace(settings_module.settings,
        database_url='postgresql://test.invalid/test', aemet_public_base_url='https://test.example'))
    monkeypatch.setattr(aemet, 'fetch_public_aemet_json', lambda *args: {
        'local_date': target.isoformat(), 'provider_status': 'success',
        'latest_observation': {'observed_at': now.isoformat(), 'temperature_c': 37.5},
        'physical_tmax': {'observed_at': now.isoformat(), 'value_c': 37.6},
        'observations': [{'observed_at': now.isoformat(), 'temperature_c': 37.5}],
    })
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run()
    app.sidebar.date_input[0].set_value(target).run()
    assert not app.exception, [item.message for item in app.exception]
    headings = [item.value for item in app.subheader]
    assert headings.index('AEMET 3129 · Physical station observations') == headings.index('3 · Relevant buckets') + 1
    labels = [item.label for item in app.metric]
    assert 'Tmax report time' in labels and 'Feed status' in labels
    assert 'Tmax time' not in labels
