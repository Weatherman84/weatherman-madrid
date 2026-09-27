# Installation Weatherman Madrid v1.0.17

## Umfang

v1.0.17 ergänzt ausschließlich Research-Code und einen manuellen Export. Forecast
Engine v10.7.11, geschützte Baseline v10.7.10, Production Champion, Trading Shadow,
Neon-Schema und Cloudflare Worker bleiben unverändert.

## 1. GitHub und Streamlit

Nach dem Merge des v1.0.17-Pull-Requests:

1. Unter **Actions → 0 - Tests** den erfolgreichen Lauf prüfen.
2. Streamlit Community Cloud sollte den neuen `main`-Stand automatisch deployen.
3. Im App-Footer `App v1.0.17`, `Engine v10.7.11` und
   `protected forecast baseline v10.7.10` prüfen.

Es gibt keine Neon-Migration, keine neuen Secrets und keine Cloudflare-Änderung.

## 2. Transferfreien Dry Run ausführen

Unter **Actions → 12 - Export Madrid checkpoint research → Run workflow**:

- `days`: `30`
- `end_date`: leer lassen
- `dry_run`: aktiviert lassen

Der Log muss unter anderem zeigen:

- `production_queries_executed: 0`
- maximal 120 Checkpointzeilen
- maximal 2.880 kompakte Modelllaufzeilen
- maximal 240 TAF-Zeilen
- geschätzte Exportgröße ungefähr 900.000 Bytes

Der Dry Run erzeugt absichtlich kein Artefakt und liest Neon nicht.

## 3. Einmaligen Research-Export erzeugen

Erst wenn der Dry Run plausibel ist, Workflow 12 erneut mit `dry_run` deaktiviert
starten. Der Workflow verwendet eine explizite Read-only-Transaktion und führt am
Ende immer ein Rollback aus. Danach das Artefakt
`madrid-checkpoint-research-v0.1` herunterladen; es enthält
`checkpoint-research-export.json`.

Der Export wird nicht bei App-Starts oder zeitgesteuert ausgeführt. Für weitere
Hypothesen soll dieselbe Datei lokal im Replay-Chat verwendet werden, ohne Neon
erneut abzufragen.

## 4. D−1 v0.2

Workflow **11 - Export Madrid D-1 evening research** enthält zusätzlich zum
unveränderten v0.1 nun v0.2 und den direkten Vergleich. Auch dort zuerst den Dry Run
verwenden. Alte v0.1-Ergebnisse werden nicht überschrieben oder rückwirkend geändert.

## Sicherheitsbestätigung

- `research_only=true`
- `automatic_promotion=false`
- Production Neon ausschließlich read-only
- keine automatischen Backfills
- keine Full-Table-Scans oder Modell-Grids
- keine Änderung der Forecast- oder Trading-Production-Logik
