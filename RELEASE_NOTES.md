# Release Notes – Madrid v1.0.1

## Neu in v1.0.1

- täglicher, Madrid-only Read-only-Export aus Neon für die Research-Analyse;
- stabile GitHub-Pages-URL statt Neon-Zugangsdaten in ChatGPT;
- Checkpoints mit Recorded-at, Evidence Class, Freshness und Forecast Ladder;
- Forecast Drivers, einzelne Adjustment-/Regime-Impacts und Regime Memory;
- Champion/Challenger-Buckets, TAF-Provenienz, Actuals und METAR-Verlauf;
- Collector-Run- und Quellenabdeckung zur Diagnose fehlender Schedule-Dispatches;
- fail-closed Credential-Prüfung und pseudonymisierte Collector-Referenzen;
- Forecast-Engine weiterhin byteidentisch v10.7.10.

## Weiterhin bewusst getrennt

- Der bestehende Workflow 5 bleibt ein Snapshot-Replay-Pilot und wird nicht als
  vollständiger externer 360-Tage-Archive-Replay dargestellt.
- Keine Änderung von Formel, Bias, Gewichten, Regimen, Locks oder Promotion-Gates.

## Bereits enthalten seit v1.0.0

## Enthalten

- Madrid-only Streamlit Trading Desk;
- Neon/PostgreSQL Production Store;
- getrenntes Neon Replay Lab;
- vier fixe Checkpoints mit Zeitstempeln;
- maximal vier checkpointgebundene Meteoblue-Versuche pro Tag;
- 15-Minuten-Collector im aktiven Fenster und stündlicher Safety Collector;
- direkte, synchrone Datenbasis für Collector und App;
- Exact Bucket, ±1 °C, MAE, Bias, N und N-Erklärung;
- temperatur-sortierte relevante Buckets;
- stale Quellen werden vom Champion ausgeschlossen;
- manueller 30-Tage-Replay mit herunterladbarem JSON-Bericht;
- LEMD-only Import der bestehenden Historie ohne Datenfiles im neuen Repository.

## Nicht enthalten

- keine Änderung der Forecast-Engine-Formel;
- keine neue Bias-, Weight-, Regime- oder Lock-Regel;
- keine Challenger-Promotion;
- keine produktive Replay-Promotion;
- keine Airport-Research-Seite;
- keine zusätzlichen Airports;
- kein automatischer täglicher Replay.
