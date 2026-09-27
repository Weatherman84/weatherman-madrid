# Validation – Weatherman Madrid v1.0.15

## Ergebnis

- `pytest -q`: **270 passed**, drei bestehende NumPy-Deprecation-Warnungen.
- `ruff check app.py src tests scripts`: **passed**.
- `python -m compileall -q app.py src scripts tests`: **passed**.
- Cloudflare Worker: **11/11 tests passed**.
- Alle GitHub-Workflow-Dateien erfolgreich als YAML geparst.
- Reale Daily-Analysis-Datei mit 2.378.916 Bytes testweise verarbeitet; die datierte
  Kopie war byte-identisch und der SHA-256 stimmte überein.

## Geschützte Forecast-Engine

`scripts/verify_engine_baseline.py` bestätigt **30/30 geschützte Dateien
byte-identisch** zur Baseline v10.7.10. Export-Engine bleibt v10.7.11.

Nicht geändert wurden insbesondere Champion, Bias, Madrid-Anchor, Live Correction,
TAF-Stufe, Day-/Peak-Lock und produktive Regimegewichte.

## v1.0.12 kumulativ enthalten

- Trading-Decisions speichern Checkpointstatus und Rekonstruktionskennzeichen.
- Rekonstruierte Checkpoints sind `reconstructed_research`, nicht Historical OOS.
- Regime-Matrix trennt `scheduled_causal_only`, `reconstructed_research` und
  `all_research_evidence`; Standard ist `scheduled_causal_only`.

## v1.0.13

- Workflow 6 erzeugt genau einen Neon-Export und veröffentlicht dieselben geprüften
  Bytes als GitHub-Pages-Latest, datierte Pages-Datei und Cloudflare-KV-Mirror.
- Der Mirror führt **0 zusätzliche Production-Neon-Abfragen** aus.
- Veröffentlichung protokolliert Zieltag, Erzeugungszeit, Dateigröße, SHA-256 und
  Verifikationsstatus beider Endpunkte.
- Der Worker begrenzt Uploads auf 5 MiB; datierte KV-Kopien laufen nach 90 Tagen ab.
- `late_live_overlap_guard_candidate_v0.1` erkennt nur den Research-Fall aus
  Clear-Sky/Cloud/Radiation, passiertem Modellpeak und TAF-Disagreement. Er berechnet
  keinen Cap, keine neue Wahrscheinlichkeit und hat `champion_impact_c=0.0`.

## v1.0.14

- Das produktive KV-Binding `AEMET_HOT` ist samt Namespace-ID in der Wrangler-
  Konfiguration versioniert.
- Der Workflow-/Konfigurationstest schützt Binding und Namespace-ID vor Regressionen.
- Der Fix führt keine Neon-Abfrage aus und ändert keine Forecastkomponente.

## v1.0.15

- Track C exportiert ausschließlich `D-1 Evening @20:00` als kompakten,
  kausalen Research-Datensatz.
- Der historische Challenger verwendet einen expandierenden Walk-forward-Pool
  mit mindestens zehn früheren `scheduled-causal` Fällen und maximal ±0,5 K
  Research-Anpassung.
- D−1-Regime-Matrix und Fehlfallanalyse trennen `scheduled_causal`,
  `reconstructed_research`, späteres `sequential_oos` und `live_shadow`.
- Dry-run für 30 Tage: **0 Production-Abfragen**, maximal 30 Checkpointzeilen,
  1.440 kompakte Modellzeilen und 240 TAF-Metadatenzeilen; geschätzter Export
  etwa 240.000 Bytes.
- Der reale Export läuft nur manuell über Workflow 11 und wird nie beim Öffnen
  der App oder als automatischer Backfill gestartet.

## Noch extern zu bestätigen

Nach Installation muss Workflow 6 beide Endpunkte als `verified` melden. Danach muss
der Worker-Endpunkt einmal tatsächlich aus der ChatGPT-Automationsumgebung gelesen
werden. Vor diesem externen Test gilt der zweite Abrufweg als implementiert, aber noch
nicht als automationstauglich freigegeben.

Die tatsächliche Zahl verfügbarer D−1-Fälle, die Modellabdeckung und der erste reale
Champion-Challenger-Vergleich können erst nach dem einmaligen, expliziten
`dry_run=false` von Workflow 11 berichtet werden; während der Codevalidierung wurde
Production Neon bewusst nicht geöffnet.
