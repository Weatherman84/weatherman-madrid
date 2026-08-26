# Weatherman Madrid

Eine eigenständige, ressourcenschonende Streamlit-App für **LEMD / Madrid-Barajas**.
Die Forecast-Engine bleibt auf dem produktiven Stand **v10.7.10** eingefroren; die
aktuelle Madrid-App-Version heißt **v1.0.1**. Das neue Repository und die neuen Neon-
Datenbanken sind vollständig vom bisherigen Sechs-Airport-System getrennt.

## Was diese Version löst

- nur ein Airport statt Airport-Research und sechs parallelen Trading-Pfaden;
- PostgreSQL/Neon statt einer wachsenden SQLite-Datei im Git-Repository;
- ein gemeinsamer Datenstand für Collector und Streamlit-Refresh;
- vier eindeutig benannte lokale Entscheidungspunkte;
- maximal ein Meteoblue-Versuch je Checkpoint, transparent protokolliert;
- Reliability mit erklärtem `N`, Exact Bucket, ±1 °C, MAE und Datenstand;
- ein manueller, isolierter 30-Tage-Replay mit getrennten Evidenzklassen;
- ein täglicher, bereinigter Read-only-Export für die Madrid-Research-Analyse;
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

- Aktiver Collector: alle 15 Minuten im relevanten UTC-Fenster.
- Sicherheitsabdeckung: stündlich außerhalb dieses Fensters.
- Daten werden direkt nach Neon geschrieben; es gibt keinen Git-Commit/Push einer DB.
- Das öffentliche Repository bleibt klein und enthält keine historische Datenmenge.
- Die bestehende App bleibt als unabhängiger Rollback bestehen.

## Replay-Evidenz

- `historical-causal`: Die verwendeten Quelldaten waren nachweislich bereits vor dem
  Checkpoint abgerufen.
- `reconstructed-research`: Die Daten waren fachlich zum Checkpoint verfügbar, wurden
  aber erst später abgerufen und anschließend kausal rekonstruiert.
- `unavailable`: Es existieren nicht mindestens zwei frische, kausal nutzbare Modelle.

Diese Klassen werden nie vermischt. Replay-Ergebnisse sind `RESEARCH ONLY` und ändern
weder Produktionsdaten noch Forecast-Gewichte, Regime, OOS-Zähler oder Promotionen.

Der bestehende Replay ist ein Pilot auf den in Production gespeicherten Snapshots. Er ist
kein vollständiger 360-Tage-Neuaufbau aus externen Wetterarchiven. Ein solcher Archive
Replay bleibt eine getrennte spätere Ausbaustufe.

## Täglicher Analyseexport

Workflow **6 - Publish Madrid daily-analysis export** liest sieben Madrid-Tage in einer
explizit schreibgeschützten Neon-Transaktion. Er veröffentlicht ausschließlich den
bereinigten Research-Datensatz unter:

`https://weatherman84.github.io/weatherman-madrid/daily-analysis-latest.json`

Enthalten sind die festen Checkpoints, Zeitstempel, Evidence/Freshness, Forecast Ladder,
Buckets, Forecast Drivers, Adjustment- und Regime-Impacts, Champion/Challenger,
TAF-Provenienz, Actuals sowie Collector-Abdeckung. Connection Strings, Passwörter und
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
- [meteoblue Free Weather API](https://docs.meteoblue.com/en/weather-apis/free-weather-api/overview)
