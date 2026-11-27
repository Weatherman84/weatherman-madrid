"""Annotate existing gate pairs without changing its thresholds or predictions."""
from collections import Counter
from datetime import timedelta
import json

import pandas as pd


def annotate_report(report, snapshots=None):
    rows = snapshots.to_dict('records') if snapshots is not None and not snapshots.empty else []
    for gate in report['gates'].values():
        classes = Counter()
        for day in gate['used_days']:
            at = pd.Timestamp(day['captured_at'])
            matches = [r for r in rows if str(r.get('airport')) == day['airport']
                       and str(r.get('target_date'))[:10] == day['target_date']
                       and pd.Timestamp(r['captured_at']) == at]
            state, receipts = 'unknown', 'unknown'
            if len(matches) == 1:
                row = matches[0]
                reconstructed = row.get('checkpoint_reconstructed')
                if reconstructed is True:
                    state = 'reconstructed'
                elif reconstructed is False and row.get('checkpoint_status') == 'scheduled-causal':
                    recorded = pd.to_datetime(row.get('checkpoint_recorded_at'), utc=True, errors='coerce')
                    if pd.notna(recorded) and at <= recorded <= at.to_pydatetime() + timedelta(minutes=35):
                        state = 'scheduled_recording'
                def array(value):
                    try:
                        parsed = json.loads(value) if isinstance(value, str) else value
                        return parsed if isinstance(parsed, list) else []
                    except (ValueError, TypeError):
                        return []
                sources = array(row.get('source_provenance_json'))
                used = set(array(row.get('used_models_json')))
                selected = [s for s in sources if s.get('model') in used]
                if used and {s.get('model') for s in selected} == used and all(s.get('fetched_at') for s in selected):
                    receipts = 'after_cutoff_present' if any(pd.Timestamp(s['fetched_at']) > at for s in selected) else 'model_receipts_before_cutoff'
            day['recording_provenance'] = state
            day['model_receipt_provenance'] = receipts
            classes[state] += 1
        gate['provenance_review'] = {
            'recording_counts': dict(classes), 'checked_pairs': len(gate['used_days']),
            'release_authorized': False,
            'strict_input_provenance_certified': False,
            'reason': 'Recording labels and provider availability alone do not prove the complete received information set. No promotion is performed.'}
    return report
