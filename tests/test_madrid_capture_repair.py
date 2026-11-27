"""New source semantics, recording failures and existing-table rendering."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from weatherman.madrid_coherent_inputs import (
    CONTRACT_SHA, SOURCE_CLASS, parse_model, request_params,
)
from weatherman.madrid_late_shadow_display import component_views, stored_late_champion, table_view_for_target
from weatherman.madrid_late_shadow_inputs import build_input
from weatherman.madrid_late_shadow_journal import append_record, build_journal, decision, validate_decision
from weatherman.madrid_late_shadow_model import MODELS, bounds, cutoff
from weatherman.shadow_capture_health import CaptureRejected, append_attempt, checkpoint_recording_health
from test_madrid_late_shadow_live import sources, frozen

ROOT = Path(__file__).resolve().parents[1]


def coherent_sources(day='2026-11-28'):
    day, captured, raw = sources(day)
    start, end = bounds(day)
    times = [int((start + timedelta(hours=i)).timestamp()) for i in range(int((end-start).total_seconds()/3600))]
    payload = {'hourly': {'time': times, 'temperature_2m': [21.] * len(times)},
               'daily': {'temperature_2m_max': [21.]}}
    metadata = {'last_run_initialisation_time': int((captured - timedelta(hours=8)).timestamp()),
                'last_run_availability_time': int((captured - timedelta(hours=1)).timestamp())}
    forecasts, hourly, archive = [], [], []
    for model in MODELS:
        f, h = parse_model(day, model, payload, metadata, metadata, captured)
        forecasts.append(f)
        hourly.extend(h)
        archive.append({'model': model, 'received_at': captured.isoformat(),
                        'request': {**request_params(day), 'models': model},
                        'raw_forecast_text': json.dumps(payload), 'metadata_before_text': json.dumps(metadata),
                        'metadata_after_text': json.dumps(metadata)})
    raw.update(forecasts=forecasts, hourly=hourly)
    row = build_input(day, captured, **raw)
    row['input_source_class'] = SOURCE_CLASS
    row['curve_audit']['source'] = SOURCE_CLASS
    row['coherent_input_bundle'] = {'target_date': day, 'source_class': SOURCE_CLASS,
                                    'source_contract_sha256': CONTRACT_SHA, 'models': archive}
    return row, captured, payload, metadata


@pytest.mark.parametrize('day', ['2026-11-28', '2027-07-10', '2027-03-28', '2027-10-31'])
def test_coherent_local_day_keeps_frozen_math_and_distinct_evidence(day, tmp_path):
    row, captured, _, _ = coherent_sources(day)
    assert row['curve_available']
    r = decision(row, captured_at=captured, generated_at=captured+timedelta(seconds=1), run_id='v2')
    assert validate_decision(r) == r
    append_record(tmp_path, 'decisions', r)
    j = build_journal(tmp_path)
    assert j['evidence_by_input_source_class'][SOURCE_CLASS]['curve_evidence']['frozen_days'] == 1
    bad = deepcopy(row)
    bad['coherent_input_bundle']['models'][0]['request']['timezone'] = 'UTC'
    with pytest.raises(ValueError, match='request'):
        decision(bad, captured_at=captured, generated_at=captured+timedelta(seconds=1), run_id='bad')


@pytest.mark.parametrize('problem', ['hole', 'duplicate', 'metadata_update', 'late'])
def test_missing_hour_or_changed_latest_product_is_rejected_without_older_substitute(problem):
    row, captured, payload, metadata = coherent_sources()
    after = dict(metadata)
    if problem == 'hole':
        payload['hourly']['time'].pop(0)
        payload['hourly']['temperature_2m'].pop(0)
    elif problem == 'duplicate':
        payload['hourly']['time'][1] = payload['hourly']['time'][0]
    elif problem == 'metadata_update':
        after['last_run_initialisation_time'] += 3600
    else:
        captured = cutoff(row['target_date'])
    with pytest.raises(CaptureRejected):
        parse_model(row['target_date'], MODELS[0], payload, metadata, after, captured)


def test_attempt_is_not_an_oos_day_and_ui_explains_the_gap(tmp_path):
    append_attempt(tmp_path, day='2026-11-27', run_id='failed', reason='latest_fetch_too_old', details={'model': MODELS[0]})
    j = build_journal(tmp_path, as_of='2026-11-27T16:00:00Z')
    assert not j['decisions'] and not j['outcomes']
    assert j['curve_evidence']['frozen_days'] == 0
    assert 'older than six hours' in table_view_for_target('2026-11-27', live=j)['caption']


def test_v1_fallback_cannot_be_mislabelled_as_active_curve(tmp_path):
    day, at, raw = sources()
    raw['hourly'].pop()
    r = decision(build_input(day, at, **raw), captured_at=at, generated_at=at+timedelta(seconds=1), run_id='fallback')
    append_record(tmp_path, 'decisions', r)
    view = table_view_for_target(day, live=build_journal(tmp_path))
    parts = component_views(view)
    assert not parts['v11 Curve · Late']['probabilities']
    assert parts['v11 Reference · Late']['probabilities'] == parts['v11 Fallback · Late']['probabilities']
    assert view['record']['input_source_class'] == 'live_point_in_time_composite_v1'


def test_bucket_comparison_never_substitutes_another_time_or_day():
    f = pd.DataFrame([{'airport': 'LEMD', 'target_date': '2026-11-28', 'captured_at': cutoff('2026-11-28'),
                       'variant': 'Champion', 'probabilities_json': '{"12": 1}'}])
    assert stored_late_champion(f, '2026-11-28') == {12: 1}
    assert not stored_late_champion(f, '2026-11-27')
    assert not stored_late_champion(pd.concat([f, f]), '2026-11-28')


def test_slot_success_does_not_hide_missing_forecast():
    health = checkpoint_recording_health([], generated_at='2026-11-27T21:00:00Z', target_date='2026-11-27')
    assert health['missing_count'] == 3
    assert all(c['status'] == 'missing_recording' for c in health['checkpoints'])


def wrapper():
    spec = importlib.util.spec_from_file_location('checked_collector', ROOT/'scripts/run_madrid_collector_checked.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preparation_retries_before_cutoff_but_never_creates_forecasts():
    m = wrapper()
    at = cutoff('2026-11-28') - timedelta(hours=7, minutes=23)  # 08:37 Madrid
    calls = []
    def refresh(now):
        calls.append(now)
        return {'forecasts': 0, 'hourly_forecasts': 0} if len(calls) == 1 else {
            'forecasts': 2, 'hourly_forecasts': 48,
            'committed_receipts': [{'model': model, 'fetched_at': now.isoformat()} for model in MODELS[:2]]}
    assert m.prepare(at, refresh=refresh, clock=lambda: at)['status'] == 'prepared'
    assert len(calls) == 2
    assert m.prepare(cutoff('2026-11-28'), refresh=refresh)['status'] == 'not_due'
    assert len(calls) == 2


def test_latest_stale_reason_has_safe_exact_timestamps():
    day, at, raw = sources()
    raw['forecasts'][0]['fetched_at'] = (at-timedelta(hours=7)).isoformat()
    raw['forecasts'][0]['run_at'] = (at-timedelta(hours=8)).isoformat()
    with pytest.raises(CaptureRejected) as exc:
        build_input(day, at, **raw)
    assert exc.value.reason == 'latest_fetch_too_old'
    assert set(exc.value.details) == {'model', 'fetched_at', 'run_at', 'captured_at'}


def test_daily_hourly_disagreement_remains_exact_reference_fallback():
    day, captured, raw = sources()
    raw['forecasts'][0]['max_temp_c'] += .1
    row = build_input(day, captured, **raw)
    from weatherman.madrid_late_shadow_model import predict
    assert not row['curve_available']
    assert row['curve_audit']['missing_reasons'][MODELS[0]] == 'daily_hourly_maximum_mismatch'
    assert predict(row)['candidate'] == predict(row)['reference']


def test_rejected_fetch_is_journalled_even_when_no_database_is_available(tmp_path, monkeypatch):
    import sys
    from datetime import datetime
    spec = importlib.util.spec_from_file_location('capture_runner', ROOT/'scripts/record_madrid_late_shadow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff('2026-11-27') - timedelta(minutes=5)
    def reject(day):
        raise CaptureRejected('model_source_unavailable', 'unavailable')
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'fetch_models', reject)
    monkeypatch.setenv('V11_INPUT_SOURCE', SOURCE_CLASS)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(sys, 'argv', ['record', '--store', str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    journal = json.loads((tmp_path/'journal.json').read_text())
    assert journal['capture_attempts'][0]['reason'] == 'model_source_unavailable'
    assert not journal['decisions'] and not journal['outcomes']
    workflow = (ROOT/'.github/workflows/madrid-v11-shadow.yml').read_text()
    assert 'continue-on-error: true' in workflow and 'decisions outcomes attempts' in workflow
    assert workflow.index('Publish decisions') < workflow.index('Fail visibly')


def test_provenance_annotation_never_changes_numerical_gate_or_certifies_late_receipts():
    from weatherman.analog_recording_provenance import annotate_report
    at = cutoff('2026-11-27')
    report = {'gates': {'late': {'status': 'AUTO-PROMOTION ELIGIBLE', 'metrics': {'mae_gain_c': .18},
               'used_days': [{'airport': 'LEMD', 'target_date': '2026-11-27', 'captured_at': at.isoformat()}]}}}
    snapshots = pd.DataFrame([{'airport': 'LEMD', 'target_date': '2026-11-27', 'captured_at': at,
        'checkpoint_recorded_at': at+timedelta(minutes=7), 'checkpoint_reconstructed': False,
        'checkpoint_status': 'scheduled-causal', 'used_models_json': '["ecmwf_ifs025"]',
        'source_provenance_json': json.dumps([{'model': 'ecmwf_ifs025', 'fetched_at': (at+timedelta(minutes=7)).isoformat()}])}])
    gate = annotate_report(report, snapshots)['gates']['late']
    assert gate['status'] == 'AUTO-PROMOTION ELIGIBLE' and gate['metrics']['mae_gain_c'] == .18
    assert gate['used_days'][0]['recording_provenance'] == 'scheduled_recording'
    assert gate['used_days'][0]['model_receipt_provenance'] == 'after_cutoff_present'
    assert not gate['provenance_review']['release_authorized']


def test_v2_evidence_does_not_relabel_or_pool_legacy_records(tmp_path):
    old = frozen('2026-11-24')
    append_record(tmp_path, 'decisions', old)
    path = tmp_path/'decisions/2026-11-24.json'
    original = path.read_bytes()
    row, captured, _, _ = coherent_sources('2026-11-28')
    new = decision(row, captured_at=captured, generated_at=captured+timedelta(seconds=1), run_id='v2')
    append_record(tmp_path, 'decisions', new)
    j = build_journal(tmp_path)
    assert path.read_bytes() == original
    assert j['evidence_by_input_source_class'][SOURCE_CLASS]['curve_evidence']['frozen_days'] == 1
    assert j['evidence_by_input_source_class']['live_point_in_time_composite_v1']['curve_evidence']['frozen_days'] == 1
    assert j['aggregate_metrics_classification'] == 'mixed_input_versions_diagnostic_only'
