"""Operational diagnostics; never forecasts, model promotion or database writes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


class CaptureRejected(ValueError):
    def __init__(self, reason, message, **details):
        super().__init__(message)
        self.reason, self.details = reason, details


def append_attempt(root, *, day, run_id, reason, details=None):
    from .madrid_late_shadow_journal import seal
    from .madrid_late_shadow_model import digest
    record = seal({'target_date': str(day), 'run_id': str(run_id), 'reason': reason,
                   'recorded_at': datetime.now(timezone.utc).isoformat(),
                   'classification': 'operational_attempt_not_forecast', 'details': details or {}})
    folder = Path(root) / 'attempts'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{day}--{digest(str(run_id))[:16]}.json'
    if path.exists():
        return
    with path.open('x') as stream:
        json.dump(record, stream, sort_keys=True, allow_nan=False)


def checkpoint_recording_health(checkpoints, *, generated_at, target_date):
    """Assess persisted rows, independently of collection slot status."""
    from .madrid_late_shadow_model import ZONE, instant
    now = instant(generated_at)
    result = []
    for label, hour in [('D0 Morning @09:00', 9), ('First Live @12:00', 12), ('Late Live @16:00', 16)]:
        cut = datetime.fromisoformat(str(target_date)).replace(hour=hour, tzinfo=ZONE)
        matches = [c for c in checkpoints if c.get('target_date') == str(target_date)
                   and c.get('checkpoint') == label]
        if matches:
            status = 'recorded' if len(matches) == 1 else 'duplicate_records'
        else:
            status = 'missing_recording' if now >= cut + timedelta(minutes=35) else 'pending'
        result.append({'target_date': str(target_date), 'checkpoint': label, 'status': status,
                       'recorded_at': matches[0].get('recorded_at') if len(matches) == 1 else None})
    return {'classification': 'recording_coverage_not_forecast_skill', 'checkpoints': result,
            'missing_count': sum(r['status'] == 'missing_recording' for r in result)}
