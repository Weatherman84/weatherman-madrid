# Installation Weatherman Madrid v1.0.18

## Umfang

v1.0.18 ergänzt ausschließlich lokale Research-/Shadow-Auswertungen im kompakten
Checkpoint-Export. Forecast Engine v10.7.11, geschützte Baseline v10.7.10,
Production Champion, Neon-Schema, Trading-Production und Cloudflare Worker bleiben
unverändert.

## 1. GitHub und Streamlit

Nach dem Merge des v1.0.18-Pull-Requests:

1. Unter **Actions → 0 - Tests** den erfolgreichen Lauf prüfen.
2. Streamlit Community Cloud sollte den neuen `main`-Stand automatisch deployen.
3. Im App-Footer `App v1.0.18`, `Engine v10.7.11` und
   `protected forecast baseline v10.7.10` prüfen.

Es gibt keine Neon-Migration, keine neuen Secrets und keine Cloudflare-Änderung.
Die Research-Sektion zeigt die neuen Pfade aus bereits gecachten Cockpit-Daten und
erzeugt beim Öffnen keine zusätzliche Datenbankabfrage.

## 2. Transferfreien Dry Run ausführen

Unter **Actions → 12 - Export Madrid checkpoint research → Run workflow**:

- `days`: `30`
- `end_date`: leer lassen
- `dry_run`: aktiviert lassen

Der Dry Run muss `production_queries_executed: 0` melden. Für 30 Tage gelten
weiterhin maximal 120 Checkpoints, 2.880 kompakte Modelllaufzeilen und 240
TAF-Zeilen. Er erzeugt absichtlich kein Artefakt und liest Neon nicht.
Die konservative Größenabschätzung liegt für 30 Tage bei ungefähr 3,6 MB; sie
betrifft die lokale JSON-Datei, nicht zusätzlichen Datenbanktransfer.

## 3. Research-Export v0.2 erzeugen

Workflow 12 anschließend einmal mit deaktiviertem `dry_run` starten. Der reale Lauf
verwendet eine Read-only-Transaktion und führt am Ende ein Rollback aus. Das Artefakt
`madrid-checkpoint-research-v0.2` enthält `checkpoint-research-export.json`.

Der Export enthält:

- D−1 `d1_evening_challenger_v0.2`;
- D0 `d0_morning_challenger_v0.1`;
- First Live mit explizit geschütztem Champion und ohne Forecast-Challenger;
- Late Live `late_live_ablation_challenger_v0.1`;
- historische Walk-forward-Vergleiche und lokale Sensitivitätsmatrizen.

Für die Replay-Analyse diese Datei lokal weiterverwenden. Neue Hypothesen benötigen
keine erneute Production-Abfrage.

## 4. Sicherheitsbestätigung

- `research_only=true`
- `automatic_promotion=false`
- Production Neon ausschließlich read-only
- keine automatischen Backfills
- keine Full-Table-Scans, Modell-Grids oder Provider-Rohpayloads
- keine zusätzliche DB-Abfrage für die Challenger
- keine Änderung produktiver Forecast-, Regime-, TAF-, Lock- oder Trading-Logik
