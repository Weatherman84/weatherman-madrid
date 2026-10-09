# Historical Replay v1 – Quellenprüfung und diagnostischer Backfill v0.2.0

Diese Stufe ergänzt den historischen D−1-Kalibrierungs-Warmup und erlaubt einen
getrennten diagnostischen Partial-Fidelity-Backfill. Historisches Challenger
Tournament und Optimierungsphasen sind **noch nicht abgeschlossen**. Die App bleibt v1.0.19; Research-Version: `historical_backfill_v0.2.0`.

## Isolation

Die neuen Module importieren weder `db` noch `service`, öffnen keine SQL-Verbindung
und verwenden keine Production-Zugangsdaten. Bestehende Engine-, Regime- und
Challenger-Dateien bleiben byte-identisch. Es gibt keine Promotion oder produktiven
Parameteränderungen. Das Datenarchiv muss außerhalb des Git-Repositories liegen.

## Quellen und Grenzen

| Quelle | Umsetzung | Einschränkung |
| --- | --- | --- |
| Open-Meteo Single Runs | Exakter `model` und UTC-`run`, sechs stündliche Punktvariablen, getrennte Fehler pro Modell | Die Antwort bestätigt keine historische Veröffentlichungszeit. Konservative Kandidaten verwenden globale 6 h / regionale 3 h plus 10 min, ausdrücklich geschätzt. |
| IEM METAR | Alle Berichtstypen 1–4, rohe Ganzzahltemperatur statt nachträglich präzisiertem Sensorwert | Empfangszeiten fehlen. Ausgabe wird vollständig auf 30-Minuten-Sollmeldungen je lokalem Tag geprüft; ungeklärte Revisionen werden nicht als Live-Inputs genutzt. |
| IEM TAF | Issuance-Zeitraum, alle Rohrevisionen, TX/Validität/AMD/COR | Stationsabdeckung muss gemessen werden; ein leeres CSV beweist keine weltweite Archivlücke. Decodierte Änderungsgruppen fehlen in v0.1.0. |
| Aviation Weather Center TAF | Historische Checkpointabfragen innerhalb der letzten 30 Tage; Raw, TX und decodierte Perioden | `dbPopTime` dokumentiert Archiv-Eingang, keinen damaligen App-Empfang. Kein garantierter vollständiger Revisionstream. |
| Meteoblue | Als fehlender Input im Coverage-Bericht | Kein historischer Adapter; kein anderer Modelltyp als Ersatz. |
| AEMET 3129 | Separates, derzeit leeres Evaluationsfeld | Noch kein Download-Adapter; kein Ersatz für METAR-Actual. |
| Polymarket | Vorhandene First-/Late-Live-Hedge-Funktion für Gewichte wiederverwendet | Keine historischen ausführbaren Quotes oder offizielle Resolution: `NO_TRADE`, kein P&L. Probability-Net und weitere Trading-Strategien noch nicht integriert. |

Offizielle Dokumentation:

- https://open-meteo.com/en/docs/single-runs-api
- https://open-meteo.com/en/docs/model-updates
- https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?help
- https://mesonet.agron.iastate.edu/cgi-bin/request/taf.py?help

Wichtig: Die Single-Runs-Dokumentation nennt für die meisten Modelle **2. April
2026**, nicht 1. April. Historische IFS-HRES-Daten können Hindcasts eines späteren
Modellzyklus sein. Kein Modellwechsel von `ecmwf_ifs025` zu IFS-HRES, Reanalyse,
Best Match oder Seamless wird vorgenommen.

## Kausalität und Fidelity

`strict` akzeptiert ausschließlich explizit `verified_historical_availability`
markierte Inputs. Die vorhandenen öffentlichen Adapter besitzen derzeit solche
Zeitstempel nicht; strikte Replay-Outputs bleiben deshalb unavailable.

`conservative` erlaubt die dokumentierten Zeitannahmen für **diagnostische
Partial-Fidelity-Replays**. Es ist keine zertifizierte kausale Kohorte. Die Engine
bekommt nur vor dem Checkpoint als verfügbar ausgewählte Daten. Roh-`fetched_at`
bleibt der tatsächliche spätere Archivabruf; er wird nicht zurückdatiert. Interne
Engine-`run_at` entspricht wie beim bestehenden Replay der Input-Verfügbarkeitszeit;
die tatsächliche Modellinitialisierung bleibt separat `model_run_at`.

Ein Single Run kann die ersten lokalen Tagesstunden vor seinem UTC-Datum nicht
enthalten. Für diagnostische Tagesmaxima werden diese Stunden aus bereits kausal
freigegebenen früheren Runs **desselben Modells** ergänzt. `daily_aggregation_method`
und `daily_aggregation_sources` kennzeichnen diese Konstruktion; sie ist kein
Single-Run-Full-Fidelity-Nachweis. Unvollständige zusammengesetzte Tage werden
verworfen. Es werden keine Messwerte als Modellprognose eingefügt.

Die heutige `build_live_nowcast`-Funktion und bestehende kontinuierliche Regimeprofile
werden direkt aufgerufen. Regime-Memory wird mit leerer historischer Snapshot- und
Variantentabelle, ohne Promotion aufgerufen. Das ist ausdrücklich eine fehlende
Fidelity-Komponente. Der Warmup sammelt bis zu 90 frühere D−1-Checkpoint-Prognosen pro Modell und
paarweise vollständige, vor dem aktuellen Checkpoint verfügbare Actuals. Wegen
des Archivbeginns 2. April fehlen im frühen April Forecasts der Vorgeschichte;
unvollständige Actuals und am D−1-Abend noch nicht abgeschlossene Tage zählen
nicht als Trainingspaare. `warmup.paired_days_by_model` weist die echte Abdeckung
je Checkpoint aus. Historischer Terminalzustand, 850-hPa-Inputs und
TAF-Änderungsgruppen sind weiterhin nicht vollständig rekonstruiert. Die aktuelle
Engine wird so auf historisch verfügbaren Wetterdaten diagnostiziert; es wird keine
damalige produktive Entscheidung behauptet.

Actual wird **nach** dem Forecast angehängt. Für Bias-Training werden nur ältere
Actuals mit `available_at <= checkpoint_at` freigegeben. Die native Engine kann
zusätzlich ihren unveränderten kausalen METAR-Fallback aus bereits verfügbaren
Meldungen verwenden. Evaluiert wird ausschließlich ein Tag mit allen geplanten
30-Minuten-Meldungen. Ein unvollständiges Maximum bleibt `archive_metar_max_c` und
fließt nicht in Standardmetriken ein. Auch ein vollständiges Archivmaximum ist
kein Nachweis der offiziellen Polymarket-Resolution.

Die existing D−1/D0/Late-Live-Forecast-Challenger trainieren derzeit nur auf
`scheduled_causal`. Diese Schutzregel wird nicht umgangen; Partial-Fidelity-Daten
werden nicht in `scheduled_causal` umbenannt. Historische Challenger-Auswertung
bleibt daher in dieser Stufe gesperrt. Native Engine-Ablationsvarianten und bestehende
Hedge-Gewichte werden exportiert, ohne neue Logik zu entwickeln.

## Nutzung

```bash
python -m pip install -r requirements-historical-replay.txt
PYTHONPATH=src python scripts/historical_replay_backfill.py \
  --mode plan --start 2026-04-01 --end 2026-10-08 \
  --archive ../madrid-historical-archive

PYTHONPATH=src python scripts/historical_replay_backfill.py \
  --mode dry-run --start 2026-04-01 --end 2026-10-08 \
  --days 2026-04-04,2026-07-15,2026-09-10 \
  --archive ../madrid-historical-archive \
  --timing-policy conservative --max-requests 800 --warmup-days 90
```

Exit-Code `2` bedeutet: Qualitätsfreigabe fehlt oder Requestbudget wurde erreicht.
Dieser Code darf nicht als erfolgreich abgeschlossener Backfill interpretiert
werden. Wiederholung nutzt überprüfte Cache-Dateien. `--offline` verbietet sämtliche
Downloads. Gescheiterte Abrufe werden erst mit `--retry-failed` erneut versucht.
429 beendet den betroffenen Abruf ohne Retry-Schleife. Maximal zwei Versuche pro
Request, mindestens 1,1 s zwischen Requests, 25 s Requesttimeout.

Der Modus `backfill` benötigt einen erfolgreichen Dry Run mit mindestens drei
verschiedenen Tagen und allen vier eindeutigen Checkpoints pro Tag. Jeder Fall
muss Forecast-Wahrscheinlichkeiten, ein vollständiges Actual, bestandene
Input-Leakage-Prüfungen und Research-Isolation haben. Dieser diagnostische Gate
akzeptiert ausdrücklich Partial Fidelity. Der separate `full_fidelity_gate_passed`
verlangt weiterhin vollständige Inputs und eine Kausalitätszertifizierung.
Geschätzte Verfügbarkeit wird niemals zu historisch belegter Verfügbarkeit
umetikettiert. Freigaben müssen zur gesamten Engine-/Adapter-/CLI-Version,
Timing-Policy und Warmup-Konfiguration passen.

```bash
PYTHONPATH=src python scripts/historical_replay_backfill.py \
  --mode backfill --start 2026-04-01 --end 2026-10-08 \
  --archive ../madrid-historical-archive \
  --timing-policy conservative --max-requests 3000 --warmup-days 90
```

Nur abgeschlossene Tage sind zulässig. Mehrere Modelle derselben Initialisierung
werden in Punktabfragen gebündelt; Normalisierung, Runs, Verfügbarkeitsannahmen
und Fehler bleiben modellbezogen. Acht gleichzeitige Requests und vier parallele
Warmup-Tage teilen ein globales Intervall von mindestens 1,1 Sekunden. Einmal
normalisierte Modell-Runs werden mit ihrem eigenen Checksum und der ursprünglichen
Raw-Response geprüft und für überlappende Checkpoints wiederverwendet.

TAF-Quellenprüfung: IEM lieferte in den repräsentativen LEMD-Zeiträumen keine
Meldungen. Eine OGIMET-Abfrage für 9.–11. September lieferte ebenfalls keine
Bulletins; das beweist keine generelle Archivlücke. Aviation Weather Center
beschränkt die öffentliche API auf die vergangenen 30 Tage. Für diesen jüngeren
Teilzeitraum ist ein Adapter integriert: Punktabfragen pro Checkpoint, Original-
Bulletin und decodierte Änderungsgruppen; `dbPopTime` bleibt der belegte historische
Archiv-Eingang. Er ist kein Nachweis des damaligen Weatherman-Empfangs. Fehlender
Archiv-Eingang verwendet eine ausdrücklich geschätzte Issue-plus-5-Minuten-Regel.
Future/unklare Archiv-Eingänge werden nicht vor dem Checkpoint freigegeben. Bereits gecachte TAF-Bulletins bleiben auch nach Ablauf der öffentlichen
30-Tage-Aufbewahrung lokal nutzbar. Leere 204-Antworten bleiben im Raw-Archiv byte-
identisch; die Auswertung interpretiert sie ausschließlich als fehlende Bulletins.
Die API
liefert Checkpoint-Abfragen, keinen garantierten vollständigen Revisionstream. Die
April–August-Lücke bleibt offen; es gibt keine rückdatierte heutige Meldung:
https://aviationweather.gov/data/api/
https://www.ogimet.com/metars.phtml.en

## Dateien und Wiederaufnahme

- `raw/`: Inhaltshash-basierte unveränderte API-Antworten.
- `backfill_state.json`: Abrufe, Fehler, Versuche und Abdeckung verarbeiteter Tage.
- `normalized/`: METAR/TAF/Modellpfade/Actuals je Target Day als Parquet.
- `derived/checkpoints.json`: vollständige kompakte Checkpointrecords.
- `derived/historical-replay-master.parquet`: Master mit skalaren Feldern und
  JSON-kodierten verschachtelten Spalten (Wahrscheinlichkeiten, Features, Inputs).
- `results/historical-replay-summary.json`: Fidelity-getrennte diagnostische
  Baseline-Metriken, Regime-Matrix, Transfer und offene Phasen.
- `results/dry-run-gate.json`: versionierte, fail-closed Freigabe.

Die Reports zählen ausschließlich tatsächlich verarbeitete Tage. Bei Budgetabbruch
bleiben Rohdaten und normalisierte Modell-Runs erhalten. Jeder vollständig
verarbeitete Target Day wird unter `derived/<configuration-hash>/days/` atomar
persistiert. Wiederaufnahme überspringt diese Checkpoints; unabhängige Engine-/
Adapter-/Timing-/Warmup-Konfigurationen werden separat gehalten. Zusammengeführte
Masterdateien betreffen den jeweils angeforderten Zeitraum. Frühere Einzelresultate
bleiben in der konfigurationsbezogenen Tagesablage erhalten.

Zeitliche/saisonale Turniere, Counterfactual-Reruns, einmaliger Holdout und
Walk-Forward werden in der folgenden Research-Stufe umgesetzt. Der Holdout wurde
nicht zur Parameterwahl geöffnet. Baseline- und Regime-Metriken dieser Stufe sind
rein diagnostisch und wählen keinen neuen Champion.

## Tests

```bash
PYTHONPATH=src python -m pytest tests/test_historical_replay.py \
  tests/test_checkpoint_challengers_v018.py -q
python scripts/verify_engine_baseline.py
```

Prüfungen umfassen DST, vollständige lokale Modelltage, fehlende Routine-METARs,
Look-ahead in Modell/METAR/TAF-Zeitstempeln, unbekannte Revisionen, Cacheintegrität,
Requestbudget und die Wirkungslosigkeit aktueller/zukünftiger Actuals auf eine
bereits berechnete Vorhersage.
