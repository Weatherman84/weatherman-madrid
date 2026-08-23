# Project Handoff – Weatherman Madrid v1.0.0

## Basis

- Ausgangscode: öffentliches Weatherman v10.7.10, geprüfter Quell-HEAD `8b194c2`.
- Produktiver Engine-Stand: v10.7.10, unverändert.
- Neuer Produktscope: ausschließlich LEMD / Madrid-Barajas.
- Neue App-Version: v1.0.0.

## Architekturentscheidung

- Eigenes öffentliches GitHub-Repository.
- Eigene Streamlit-App.
- Neon-PostgreSQL für Produktion.
- Zweites, eigenständiges Neon-Projekt für Replay.
- Keine produktive SQLite-Datei und kein DB-Commit/Push durch Collector.
- Alte App bleibt unverändert als Rollback.

## Feste Checkpoints

1. D−1 Evening @20:00 LT.
2. D0 Morning @09:00 LT.
3. First Live @12:00 LT.
4. Late Live @16:00 LT.

Meteoblue wird höchstens einmal pro Checkpoint-Fenster versucht, maximal viermal pro
lokalem Kalendertag. Eine persistente `provider_calls`-Zeile und PostgreSQL-Sperren
verhindern Doppelverbrauch zwischen GitHub und Streamlit.

## Replay-Schutz

- Production-Verbindung wird in eine Read-only-Transaktion gesetzt und zurückgerollt.
- Ergebnisse werden ausschließlich unter `replay_lab` im Replay-Projekt gespeichert.
- Evidenzklassen bleiben `historical-causal`, `reconstructed-research`, `unavailable`.
- Keine Promotion und keine produktive Konfigurationsänderung.

## Acceptance

- alle Tests und Ruff müssen grün sein;
- Workflow-Dateien müssen syntaktisch valide sein;
- Testmigration muss ausschließlich LEMD übernehmen;
- Release-Paket muss unter 100 Dateien bleiben;
- kein DB-, Archiv-, Cache- oder Secret-File im Release;
- Replay und Production dürfen nicht dieselbe Datenbankidentität besitzen.

## Nach dem Deployment

- zwei bis drei Tage Parallelvergleich mit der alten App;
- Checkpoint-Abdeckung, Reliability-N und Provider-Calls prüfen;
- Neon Usage nach drei und sieben Tagen kontrollieren;
- erst danach über Abschaltung alter Collector-Workflows entscheiden.
