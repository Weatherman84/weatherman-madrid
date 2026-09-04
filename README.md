# Weatherman Madrid

Eine eigenständige, ressourcenschonende Streamlit-App für **LEMD / Madrid-Barajas**.
Die aktuelle Madrid-App-Version heißt **v1.0.7** und verwendet Engine **v10.7.11**.
Forecastformeln, Gewichte, Biases, Regime und Locks bleiben auf dem Stand v10.7.10;
v10.7.11 ersetzt ausschließlich die starre 90-Minuten-Modellzulassung durch eine
modellabhängige Laufkadenz. Das Repository und die Neon-Datenbanken sind vollständig
vom bisherigen Sechs-Airport-System getrennt.

## Was diese Version löst

- nur ein Airport statt Airport-Research und sechs parallelen Trading-Pfaden;
- PostgreSQL/Neon statt einer wachsenden SQLite-Datei im Git-Repository;
- ein gemeinsamer Datenstand für Collector und Streamlit-Refresh;
- vier eindeutig benannte lokale Entscheidungspunkte;
- maximal ein Meteoblue-Versuch je Checkpoint, transparent protokolliert;
- Reliability mit erklärtem `N`, Exact Bucket, ±1 °C, MAE und Datenstand;
- ein manueller, isolierter 30-Tage-Replay mit getrennten Evidenzklassen;
- ein täglicher, bereinigter Read-only-Export für die Madrid-Research-Analyse;
- pro Modell der neueste kausal verfügbare Lauf innerhalb seiner offiziellen Kadenz;
- getrennte Status `current_latest_run`, `awaiting_next_run`, `missing_expected_run`
  und `hard_stale` statt eines pauschalen 90-Minuten-Ausschlusses;
- Cloudflare als primärer kostenloser 30-Minuten-Aviation-Scheduler;
- vollständige Modell-/Nowcastläufe nur an vier Fix-Checkpoints und zum Tagesabschluss;
- jeder manuelle Refresh als zusätzlicher kausaler `manual-live`-OOS-Snapshot;
- identische Modellzyklen werden nicht bei jedem Abruf als neue Vollkopie gespeichert;
- stündlicher GitHub-Fallback und ein garantierter 21:15-LT-Tagesabschluss;
- offizielle AEMET-Station 3129 als unabhängige Dezimaltemperatur- und Physical-Tmax-
  Quelle mit fünfminütigem Cockpit-Fragment und zehnminütigem Cloudflare-Abruf;
- kleiner Cloudflare-KV-Hot-Store und komprimierte AEMET-Tagesarchive ohne zusätzliche
  Neon-Abfragen, Modellabrufe oder GitHub-Workflow-Läufe;
- strikte Trennung von AEMET Physical Tmax, gespeichertem METAR-Maximum und einem erst
  nach bestätigter Marktregel zulässigen `market_resolution_actual`;
- zwei getrennte, unkalibrierte Shadow-Diagnosen für physischen Stall und
  METAR-Bucket-Persistenz – ohne Champion- oder Markt-Impact;
- AEMET-/METAR-Differenzen werden nur als Reihenlücken, niemals automatisch als
  Sensor-Bias interpretiert;
- kein produktiver Schreibzugriff durch den Replay und keine automatische Promotion.

## Feste Madrid-Checkpoints

| Checkpoint | Lokale Zielzeit | Meteoblue-Fenster | Zweck |
|---|---:|---:|---|
| D−1 Evening | 20:00 am Vortag | 19:45–20:30 | Vorabend-Entscheidung |
| D0 Morning | 09:00 | 08:00–09:30 | erster morgendlicher Check |
| First Live | 12:00 | 11:45–12:30 | erster fixer Live-Stand |
| Late Live | 16:00 | 15:45–16:30 | später, meist stärkerer Live-Stand |

Alle Zeiten sind `Europe/Madrid` und berücksichtigen Sommer-/Winterzeit. Mehrere
Refresh-Klicks innerhalb desselben Fensters verbrauchen keinen weiteren
Meteoblue-Versuch. METAR, TAF, Open-Meteo und Polymarket können trotzdem erneut
aktualisiert werden.

## Betrieb

- Leichter Aviation-Collector: alle 30 Minuten im relevanten UTC-Fenster.
- Vollständige Modell-, Champion- und Regimeberechnung: an den vier Fix-Checkpoints,
  zum Tagesabschluss und bei manuellem Refresh.
- Sicherheitsabdeckung: stündlich außerhalb dieses Fensters.
- Daten werden direkt nach Neon geschrieben; es gibt keinen Git-Commit/Push einer DB.
- Das öffentliche Repository bleibt klein und enthält keine historische Datenmenge.
- Die bestehende App bleibt als unabhängiger Rollback bestehen.

## Replay-Evidenz

- `historical-causal`: Die verwendeten Quelldaten waren nachweislich bereits vor dem
  Checkpoint abgerufen.
- `reconstructed-research`: Die Daten waren fachlich zum Checkpoint verfügbar, wurden
  aber erst später abgerufen und anschließend kausal rekonstruiert.
- `unavailable`: Es existieren nicht mindestens zwei kadenzgültige, kausal nutzbare
  Modelle.

Diese Klassen werden nie vermischt. Replay-Ergebnisse sind `RESEARCH ONLY` und ändern
weder Produktionsdaten noch Forecast-Gewichte, Regime, OOS-Zähler oder Promotionen.
Da v10.7.11 die Champion-Eingangsmenge ändert, beginnt sein sequenzieller OOS-Zähler
am 31. August 2026 neu; frühere v10.7.10-Tage werden nicht für eine Promotion angerechnet.

Der bestehende Replay ist ein Pilot auf den in Production gespeicherten Snapshots. Er ist
kein vollständiger 360-Tage-Neuaufbau aus externen Wetterarchiven. Ein solcher Archive
Replay bleibt eine getrennte spätere Ausbaustufe.

## Täglicher Analyseexport

Workflow **6 - Publish Madrid daily-analysis export** liest sieben Madrid-Tage in einer
explizit schreibgeschützten Neon-Transaktion. Er veröffentlicht ausschließlich den
bereinigten Research-Datensatz unter:

`https://weatherman84.github.io/weatherman-madrid/daily-analysis-latest.json`

Enthalten sind feste und zusätzliche manuelle Checkpoints, Zeitstempel,
Evidence/Freshness, Forecast Ladder, Buckets, Forecast Drivers, Adjustment- und
Regime-Impacts, Champion/Challenger,
TAF-Provenienz, getrennte AEMET-Physical-Observations, Actuals sowie
Collector-Abdeckung. Connection Strings, Passwörter und
interne Datenbank-IDs werden nicht ausgegeben. Der Export ist `RESEARCH ONLY`, schreibt
nicht nach Production und ändert die Engine nicht.

## Installation

Die vollständige Anleitung für GitHub, Neon, Actions, Streamlit und Replay steht in
[SETUP_GUIDE_DE.md](SETUP_GUIDE_DE.md). Bitte die Schritte dort in der angegebenen
Reihenfolge ausführen.

## Offizielle Referenzen

- [Neon Pricing](https://neon.com/pricing)
- [Streamlit Community Cloud: App deployen](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Streamlit Secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
- [GitHub Actions Runner für öffentliche Repositories](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Open-Meteo Model Updates und Verfügbarkeitsmetadaten](https://open-meteo.com/en/docs/model-updates)
- [meteoblue Free Weather API](https://docs.meteoblue.com/en/weather-apis/free-weather-api/overview)
- [AEMET OpenData](https://opendata.aemet.es/centrodedescargas/inicio)
- [Cloudflare Workers KV limits](https://developers.cloudflare.com/kv/platform/limits/)
