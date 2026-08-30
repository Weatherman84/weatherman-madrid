# Release Notes – Madrid v1.0.4

## Neu in v1.0.4

- Engine v10.7.11 verwendet je Modell den neuesten kausal verfügbaren Lauf;
- die starre 90-Minuten-Ausschlussgrenze entfällt für bekannte NWP-Modelle;
- offizielle Modellkadenz: sechs Stunden für ECMWF, GFS, ICON Global, UKMO und
  ARPEGE; drei Stunden für ICON-EU und AROME/AROME-HD;
- ein normales Veröffentlichungsfenster wird als `awaiting_next_run` weiterverwendet;
- `missing_expected_run` und `hard_stale` bleiben vom Champion ausgeschlossen;
- Checkpoint-Provenienz, Analyseexport und Cockpit zeigen Status, Kadenz, nächsten
  erwarteten Lauf und Zahl verpasster Updates;
- der sequenzielle Promotion-Zähler startet für v10.7.11 am 31. August 2026 neu;
- Forecastformeln, Biases, Gewichte, Regime, Locks und Promotion-Gates bleiben
  unverändert auf dem bisherigen Stand.

## Enthalten aus v1.0.3

- kostenloser Cloudflare Worker als primärer 15-Minuten-Scheduler;
- expliziter `scheduled_slot` für jede externe Ausführung;
- idempotenter Schutz gegen parallele Cloudflare-/GitHub-Aufrufe desselben Slots;
- GitHub-Cron bleibt als stündliches Sicherheitsnetz;
- eigener DST-sicherer 21:15-LT-Tagesabschluss mit METAR, Actual, Post-Peak und Export;
- Exportworkflow ohne reale-Uhrzeit-Gate und ohne grünen No-op;
- lokale und veröffentlichte `generated_at`-Werte müssen nach dem Deployment identisch
  sein;
- Export enthält Pipeline-Health sowie aktuelle Strahlungs- und 850-hPa-Modellwerte;
- vollständige Cloudflare-Einrichtungs- und Sicherheitsanleitung;
- Forecast-Engine weiterhin unverändert v10.7.10.

## Enthalten aus v1.0.2

- PostgreSQL-Bulk-Upserts ersetzen hunderte zeilenweise Neon-Abfragen im Live-Refresh;
- Upserts bleiben konfliktgesichert und atomar, doppelte Batch-Schlüssel werden stabil
  zusammengeführt;
- Provider-, Neon- und Checkpoint-Laufzeit werden im Cockpit getrennt ausgewiesen;
- der Polymarket-Aufruf im nachgelagerten Checkpoint-Journal ist auf einen Versuch mit
  sieben Sekunden Timeout begrenzt;
- Forecast-Engine v10.7.10 und sämtliche Forecastregeln bleiben unverändert.

## Enthalten aus v1.0.1

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
