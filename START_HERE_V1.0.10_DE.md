# Weatherman Madrid v1.0.10 – Installation von v1.0.9

## Vorprüfung

Der geprüfte Ausgangsstand ist Repository-Commit
`3e27fbd0f1279175d37f4bfbe901c57f58f57723` mit Paketversion `1.0.9`.
Vor dem Upload in GitHub prüfen, dass `pyproject.toml` dort noch `1.0.9`
ausweist. Bei einem abweichenden neueren Commit nicht blind überschreiben.

## Geänderte Dateien hochladen

Das vollständige v1.0.10-Paket kann über den Repository-Inhalt gelegt werden.
Für ein gezieltes Update sind folgende Dateien neu oder geändert:

- `.github/workflows/export-market-replay.yml`
- `scripts/export_market_replay.py`
- `src/weatherman/market_replay_export.py`
- `tests/test_market_replay_export.py`
- `app.py`
- `src/weatherman/__init__.py`
- `src/weatherman/aemet_live.py`
- `pyproject.toml`
- `ENGINE_BASELINE_SHA256.json`
- `README.md`
- `PROJECT_HANDOFF.md`
- `SETUP_GUIDE_DE.md`
- `RELEASE_NOTES.md`
- `RESEARCH_WATCHLIST_V1.0.10.md`
- `MARKET_REPLAY_EXPORT_V2_DE.md`
- `START_HERE_V1.0.10_DE.md`
- `VALIDATION_REPORT.md`

Wegen des versteckten `.github`-Ordners die Workflow-Datei separat kontrollieren:
In GitHub muss anschließend unter **Actions** der Workflow
**7 - Export Madrid market replay** erscheinen.

Commit-Vorschlag:

`Weatherman Madrid v1.0.10 – read-only market replay export`

## Streamlit

Streamlit sollte den neuen Commit automatisch deployen. In der Kopfzeile muss
danach stehen:

**App v1.0.10 · Engine v10.7.11 · protected forecast baseline v10.7.10**

Falls weiterhin v1.0.9 erscheint, unter **Manage app → Reboot app** einmal neu
starten. Keine Secrets ändern.

## Market-Export erzeugen

Unter **Actions → 7 - Export Madrid market replay → Run workflow** ausführen.
`days=30` lassen und `end_date` leer lassen. Nach Abschluss das Artefakt
`market-replay-export` herunterladen und die enthaltene JSON-Datei in den
Replay-Chat hochladen. Details: `MARKET_REPLAY_EXPORT_V2_DE.md`.

## Manuell unverändert lassen

- keine Neon-Migration erforderlich;
- kein neues GitHub-Secret erforderlich;
- keine Streamlit-Secret-Änderung erforderlich;
- keine Cloudflare-Code-, Binding- oder Cron-Änderung erforderlich;
- den bestehenden AEMET-KV-Binding nicht neu anlegen.

## Schutzprüfung

```bash
python scripts/verify_engine_baseline.py
```

Erwartet:

`PASS: 30 protected files byte-identical ... Forecast baseline v10.7.10; export engine v10.7.11.`
