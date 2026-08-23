# Release Notes – Madrid v1.0.0

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
