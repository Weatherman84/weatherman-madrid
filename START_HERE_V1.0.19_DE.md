# Installation Weatherman Madrid v1.0.19

## Umfang

v1.0.19 friert die bestehenden Forecast-Research-Challenger ein und startet ihre
echte Forward-/Shadow-Beobachtung. Forecast Engine v10.7.11, geschützte Baseline
v10.7.10 und Production Champion bleiben unverändert.

## 1. GitHub und Streamlit

Nach dem Merge des v1.0.19-Pull-Requests:

1. Unter **Actions → 0 - Tests** den erfolgreichen Lauf prüfen.
2. Das automatische Streamlit-Deployment abwarten.
3. Im Footer `App v1.0.19`, `Engine v10.7.11` und
   `protected forecast baseline v10.7.10` prüfen.

Es gibt keine neuen Secrets und kein neues KV-Binding. Der vorhandene Worker-Code
muss für den neuen Journal-Endpunkt aber einmal aktualisiert werden.

## 2. Cloudflare Worker einmal aktualisieren

1. Den Worker aus `cloudflare-scheduler` mit der versionierten `wrangler.jsonc`
   deployen, genau wie beim v1.0.14-Binding-Fix.
2. Das vorhandene Binding `AEMET_HOT` muss weiterhin auf
   `weatherman-madrid-aemet-hot` zeigen.
3. Das bestehende Secret `DAILY_ANALYSIS_PUBLISH_TOKEN` wird auch zum geschützten
   Publizieren des Journals verwendet. Kein Secret in Dateien oder Chat kopieren.
4. Der neue öffentliche Pfad `/forward-shadow-journal.json` darf vor dem ersten
   erfolgreichen Lauf noch HTTP 404 liefern.

## 3. Workflow 13 einmal starten

Unter **Actions → 13 - Publish Madrid frozen forward shadow → Run workflow** einmal
manuell starten. Der Lauf:

- liest Neon ausschließlich in einer read-only Transaktion;
- übernimmt einmalig höchstens 30 Tage kompakten Kalibrierungs-Seed;
- friert diesen Seed ein und trainiert nicht mit neuen Forward-Outcomes nach;
- schreibt nichts in Neon;
- veröffentlicht dieselben JSON-Bytes im vorhandenen Cloudflare KV;
- lädt ein 14 Tage verfügbares Audit-Artefakt hoch.

Danach `https://weatherman-madrid-scheduler.stefan-fleck.workers.dev/forward-shadow-journal.json`
öffnen. Erwartet sind unter anderem:

- `application_version: "1.0.19"`;
- `research_only: true`;
- `automatic_promotion: false`;
- `writes_production_database: false`;
- `classification: "READ-ONLY FROZEN FORWARD SHADOW JOURNAL"`.

Die ersten echten Entscheidungen erscheinen erst nach einem Fix-Checkpoint ab dem
29. September 2026. Ein leerer `decisions`-Block unmittelbar davor ist korrekt.

## 4. Laufende Automatik und Funktionsprüfung

Workflow 13 wird nach jedem expliziten Fixed-Collector-Lauf und nach dem Closeout
aufgerufen. DST-sichere GitHub-Crons sind nur das Sicherheitsnetz; ein Local-Time-
Gate lässt davon ausschließlich 09:00, 12:00, 16:00 und 20:00 Madrid durch.

Nach den nächsten echten Checkpoints im Research-Bereich prüfen:

- Status `FROZEN SHADOW` bei D−1, D0 und Late Live;
- First Live als `champion_protected_no_active_forecast_challenger`;
- `OOS N` bleibt bis zum finalen Stored-METAR-Actual unverändert;
- nach dem Tagesabschluss steigt nur die neue Sequential-OOS-Scorecard.

Workflow 12 kann weiterhin bei Bedarf den historischen v0.2-Research-Export
erzeugen. Er ist nicht die Quelle der neuen Forward-Scorecard.

## 5. Transfer- und Schutzbestätigung

- kein neues Neon-Schema und keinerlei Neon-Write;
- einmaliger Seed: maximal 30 Tage, 120 Checkpoints, 2.880 Modell- und 240 TAF-
  Zeilen entsprechend den bestehenden Exportlimits;
- der Seed bleibt unverändert; neue Sequential-OOS-Outcomes dienen nur der Bewertung;
- laufend: höchstens zwei operative Tage sowie Detaildaten nur für neue Checkpoints;
- maximal fünf neue Entscheidungsobjekte pro Zieltag im KV-Journal;
- Worker-Hard-Limit: 1 MiB Journalgröße;
- keine automatische historische Erweiterung oder Backfills.

- Production Forecast Engine unverändert
- Production Champion unverändert
- eingefrorene Challenger-Versionen
- `research_only=true`
- `automatic_promotion=false`
- keine automatischen Backfills
- keine Full-Table-Scans
- keine rückwirkende Neuberechnung gespeicherter Shadow-Entscheidungen
- Cloudflare enthält nur den additiven Journal-Endpunkt; AEMET bleibt unverändert
