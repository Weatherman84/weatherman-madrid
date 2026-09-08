"""Verify byte-identical protected files against the supplied v1.0.7 archive."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'ENGINE_BASELINE_SHA256.json').read_text())
failed = []
for name, expected in manifest['sha256'].items():
    path = root / name
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    if actual != expected:
        failed.append(name)
if failed:
    print('FAIL: protected files changed: ' + ', '.join(failed))
    sys.exit(1)
print(f"PASS: {len(manifest['sha256'])} protected files byte-identical to v1.0.7 "
      f"({manifest['source_archive_commit']}). Forecast baseline v10.7.10; export engine v10.7.11.")
