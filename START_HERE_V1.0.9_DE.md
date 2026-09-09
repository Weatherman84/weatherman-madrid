# Weatherman Madrid v1.0.9 – Installation von v1.0.8

## 1. Ausgangsstand prüfen

Öffne im produktiven GitHub-Repository `pyproject.toml` und `app.py`. Beide müssen
v1.0.8 ausweisen. In der laufenden Streamlit-App muss ebenfalls App v1.0.8 stehen.
Falls GitHub bereits neuere, hier nicht enthaltene Änderungen hat, diesen Stand vor
dem Upload erneut herunterladen und abgleichen.

## 2. GitHub-Dateien hochladen

Das Release-ZIP enthält `UPDATE_FROM_V1.0.8` mit allen für dieses Update benötigten
Dateien. Öffne im GitHub-Hauptverzeichnis **Add file → Upload files** und ziehe den
Inhalt dieses Ordners hinein. Der Ordner `UPDATE_FROM_V1.0.8` selbst darf nicht als
zusätzliche Ebene im Repository entstehen.

Prüfe vor dem Commit insbesondere diese Pfade:

- `app.py`
- `src/weatherman/cockpit_data.py`
- `src/weatherman/daily_analysis_export.py`
- `src/weatherman/aemet_live.py`
- `tests/test_v108_cockpit.py`
- `pyproject.toml`, `uv.lock` und `src/weatherman/__init__.py`
- `ENGINE_BASELINE_SHA256.json`

Commit-Vorschlag: `Weatherman Madrid v1.0.9 – modal reliability and attribution`.

Der Workflow unter `.github/workflows/test.yml` wurde nicht geändert. Das separate
Workflow-ZIP aus v1.0.8 muss für dieses Update nicht erneut hochgeladen werden.

## 3. Tests und Streamlit prüfen

Unter **Actions → 0 - Tests** muss der neue Lauf vollständig grün sein. Erwartet sind
Ruff, Python-Kompilierung, 247 Python-Tests, 9 Worker-Tests und die Meldung, dass 30
geschützte Dateien bytegleich zur v1.0.7-Referenz sind.

Streamlit sollte den GitHub-Commit automatisch übernehmen. Danach muss die Kopfzeile
**App v1.0.9 · Engine v10.7.11 · protected forecast baseline v10.7.10** zeigen. Falls
weiter v1.0.8 erscheint, unter **Manage app → Reboot app** einmal neu starten.

In **Fixed decision checkpoints** prüfen:

- Modal bucket und Top-1 probability;
- Runner-up und Top-1 gap;
- Champion center und Center bucket;
- Evidence und Freshness.

In **Champion reliability by fixed checkpoint** steht Modal-bucket hit an erster
Stelle. Center-bucket hit, Within ±1 K, MAE, Bias, N und Datenstand folgen separat.
Historische Modal-Buckets stammen aus den gespeicherten Champion-Wahrscheinlichkeiten.

## 4. Daily-Analysis-Export aktualisieren

Unter **Actions → 6 - Publish Madrid daily-analysis export → Run workflow** den Export
einmal manuell starten. Prüfe anschließend:

- `application_version: "1.0.9"`;
- `export_engine_version: "v10.7.11"`;
- `protected_forecast_baseline: "v10.7.10"`;
- in `forecast_chain_c` die Felder für additive Live-/TAF-Mittelpunkte und die
  anschließenden Konditionierungseffekte;
- vorhandene AEMET-Archive vom 6.–8. September ohne UnicodeDecodeError.

HTTP 404 bei älteren, tatsächlich fehlenden Archiven bleibt korrekt als
`archive_missing` sichtbar. Ein Lesefix kann nicht vorhandene Archive nicht erzeugen.

## 5. Cloudflare und Neon

Für v1.0.9 ist keine Cloudflare-Code- oder Cron-Änderung erforderlich. Der bestehende
AEMET-Cron `50 * * * *`, Collector `7,37 5-20 * * *` und Closeout bleiben bestehen.
Es gibt keine Neon-Migration und keine neuen Secrets. Bestehende Zugangsdaten bleiben
unverändert.

Die Forecastformel bleibt unverändert. Modal-Bucket-Auswertung, Top-1-Abstand und
Attribution sind Reporting- und Observability-Funktionen. AEMET bleibt eine getrennte
physische Stationsserie und wird nicht als Market-Actual verwendet.
