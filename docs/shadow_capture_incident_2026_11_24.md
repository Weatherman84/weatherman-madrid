# Fehlende Madrid-Shadow-Aufzeichnung am 24.11.2026

Geprüft und korrigiert am 25.11.2026; App-Code v1.0.25.

**Befund:** Das v11-Journal enthält für den 24.11. keine Entscheidung. Das WMP-Eingabearchiv enthält für diesen Tag einen Lückenvermerk, aber keinen Erfassungsversuch. Damit konnte die App keinen neuen v11-Forecast anzeigen. Ein grüner initialer Lese-Audit war kein Nachweis eines erfolgreichen Erfassungslaufs.

## Ursache und Nachweis

Das Erfassungsfenster ist 15:50 bis strikt vor 16:00 Europe/Madrid. Die ursprünglichen `workflow_run`-Trigger beider Jobs hörten nur auf `3 - Madrid collector`. Dessen Cloudflare-Takt startet tatsächlich um :07/:37. Der ebenfalls vorhandene :22/:52-Takt betrifft den Opportunity-Feed, nicht den Collector. Die zusätzlichen GitHub-Cron-Versuche lieferten am untersuchten Tag keinen Lauf innerhalb des Fensters.

| Beobachtetes Ereignis | UTC | Madrid | Einordnung |
|---|---|---|---|
| Ende Collector-Job, Run 45416122246 | 14:37:59 | 15:37:59 | Vor dem Erfassungsfenster |
| Collector-abhängige Shadow-Jobs angelegt | 14:38:07 | 15:38:07 | Zeit-Gate übersprungen |
| Ende Opportunity-Feed, Run 45418912910 | 14:53:19 | 15:53:19 | Passendes Uhrsignal, bisher nicht abonniert |
| Nächste Collector-abhängige Shadow-Jobs | 15:08:09 | 16:08:09 | Nach dem Cutoff |

Nachweise: GitHub-Actions-Run-/Jobmetadaten, die beiden getrennten Journale und die vorhandene Scheduler-Implementierung. Die beiden zentralen Job-Zeitpunkte liegen als reines Betriebs-Fixture unter `tests/fixtures/shadow_trigger_2026_11_24.json`; es enthält keine Wetterprognosen oder synthetischen OOS-Fälle.

## Korrektur

1. Beide Jobs hören zusätzlich auf den Abschluss des bestehenden Opportunity-Feeds. Dafür werden weder Cloudflare Worker noch Collector-, Scanner- oder Trading-Logik geändert.
2. Der Abschluss eines eigenen Main-Branch-Jobs dient nur als Uhrsignal, unabhängig von dessen Erfolg. Fremde Repository-/Branch-Läufe bleiben ausgeschlossen. Jeder neue Job prüft selbst das Zeitfenster und seine Eingaben.
3. Workflow-Zusammenfassungen nennen die tatsächliche Madrider Uhrzeit und ausdrücklich, ob ein Quellen-/Archivschritt ausgeführt oder außerhalb des Fensters übersprungen wurde.
4. Der v11-Index zeigt nach Ablauf fehlende Aufzeichnungstage separat. Die bestehenden Tabellen behalten für solche Tage `—`; der Hinweis benennt nun das verpasste Checkpoint-Datum. Diese Metadaten erzeugen keine Forecasts, Scores oder OOS-Tage.

Die Regressionstests kombinieren die tatsächlich beobachteten Ereigniszeiten mit dem echten Python-Gate aus beiden Workflow-Dateien. Sie prüfen außerdem beide UTC-Offsets, eine Minute Setup-Zeit nach dem Feed-Abschluss, die unveränderte harte 16-Uhr-Grenze und die Darstellung eines fehlenden Tages. Die gefrorenen Modellparameter und die geschützte Forecast-Baseline bleiben unverändert.

Der 24.11. bleibt ohne aufgezeichnete Live-Prognose. Die Korrektur erzeugt keinen nachträglichen v11-Wert. Ein zukünftiger erfolgreicher Erfassungslauf muss durch einen tatsächlichen, rechtzeitig gespeicherten Datensatz nachgewiesen werden; Trigger- und Unit-Tests allein beweisen das nicht.
