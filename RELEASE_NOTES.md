# Release Notes – Madrid v1.0.7

## Neu in v1.0.7

- drei unverwechselbare Auswertungsziele: `stored_metar_max`,
  `aemet_physical_tmax` und das bis zur Quellenbestätigung leere
  `market_resolution_actual`;
- `daily_max_series_gap_c` statt eines vermeintlichen Sensor-Bias: Die Differenz kann
  Station, Sensor, Zeitpunkt und Meldeverfahren gleichzeitig enthalten;
- zeitnahe AEMET-/METAR-Vergleiche mit Zeitabstand und ausdrücklicher Kennzeichnung
  `series_difference_not_sensor_bias`;
- unkalibrierter `physical_stall_shadow`, der steigende AEMET-Werte nicht als
  physisches Plateau missdeutet;
- getrennte `metar_bucket_persistence_shadow`-Diagnostik mit nächstem nominellen
  METAR-Termin;
- beide Shadow-Signale liefern keine Prozentwahrscheinlichkeit, solange die
  sequenzielle OOS-Evidenz nicht ausreicht, und haben garantiert 0,0 °C
  Champion-Impact;
- Export-Schema 1.3 mit getrennten Evaluation Targets und Verbot ihrer Vermischung;

## Enthalten aus der noch nicht installierten v1.0.6

- offizielle AEMET-OpenData-Station `3129` als physische Dezimalmessreihe;
- direkter zehnminütiger AEMET-Abruf im Cloudflare Worker ohne GitHub Action, Neon-
  Zugriff oder neue Modellabfrage;
- überschreibbare KV-Dateien `aemet-live.json` und `aemet-today.json` mit Deduplizierung
  nach Station und Messzeit;
- komprimiertes Tagesarchiv unter `archive/aemet/YYYY/MM/DD.json.gz`;
- isolierter AEMET-Providerfehler mit letztem erfolgreichen Stand und eigenem
  `fresh`-/`aging`-/`stale`-Status;
- fünfminütiger Streamlit-Fragment-Refresh ohne Champion-Neuberechnung oder vollständigen
  Seiten-Rerun;
- Tageskurve mit AEMET-Linie, getrennten METAR-Punkten und markiertem Physical Tmax;
- Export mit strikter Trennung von gespeichertem METAR-Maximum,
  `aemet_physical_tmax` und unbestätigtem `market_resolution_actual`;
- AEMET bleibt reine Beobachtungs-/Researchquelle und greift nicht in den Champion ein;
- Engine v10.7.11 und sämtliche Forecastregeln bleiben unverändert.

## Neu in v1.0.5

- leichter METAR-/TAF-/Actual-Lauf alle 30 Minuten statt vollständigem 15-Minuten-
  Nowcastjournal;
- vollständige automatische Berechnung nur an den vier festen Checkpoints und beim
  21:15-LT-Tagesabschluss;
- `Refresh Madrid now` persistiert den angezeigten Current Decision vollständig als
  zusätzlichen kausalen `manual-live`-OOS-Checkpoint;
- manuelle Snapshots enthalten Forecast Ladder, Buckets, Freshness, Modellprovenienz,
  Drivers, Regimes, Champion/Challenger und den Polymarket-Stand;
- `manual-live` wird separat ausgewertet und zählt nicht mehrfach in den standardisierten
  sequenziellen 30-Tage-Promotionszähler;
- bekannte Modellzykluszeiten ersetzen Fetch-Zeiten als persistente Schlüssel, sodass
  identische Modell- und Stundenläufe nicht unnötig vervielfacht werden;
- Analyseexport-Schema 1.1 trennt `checkpoints` und `manual_live_checkpoints`;
- Cockpit-Snapshotabfragen sind auf 120 Tage begrenzt;
- Pipeline-Monitoring erwartet 32 Aviation-/Fixslots plus einen Tagesabschluss;
- verspätete GitHub-Fallbacks werden triggerbasiert innerhalb von 20 Minuten dem
  kanonischen Sollslot zugeordnet; ein 21:22-Lauf zählt dadurch korrekt zum
  21:15-LT-Tagesabschluss, ohne manuelle Runs als Coverage zu zählen;
- der Export bezeichnet cadence-gültige Modelle als `usable` und schlüsselt
  `current_latest_run`, `awaiting_next_run`, `missing_expected_run` und `hard_stale`
  getrennt auf; die missverständliche Exportbezeichnung `fresh` entfällt;
- Engine v10.7.11 und sämtliche Forecastregeln bleiben unverändert.

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
