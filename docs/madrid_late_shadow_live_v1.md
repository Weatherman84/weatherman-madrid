# Madrid v11 Late Shadow – prospektive Erfassung v1

Der zuvor ausschließlich historische Kandidat erhält einen getrennten täglichen Ablauf. Madrid und Late Live bleiben der einzige Scope. Keine Modell-Promotion, Trading-Änderung, Änderung der geschützten Forecast-Baseline, Migration oder Schreibzugriff auf produktive Datenbanken. First Live bleibt unverändert.

## Eingefrorenes Modell

Die JSON-Parameter sind ein Export der bereits vorhandenen, auf 247 Discovery-Tagen eingefrorenen Modelle. Keine Neuschätzung, Parameterauswahl, saisonale Anpassung oder nachträgliche Kalibrierung. Identische Klassifikator-Zweige mit/ohne zulässigem TAF-TX, identischer Imputer, Scaler, empirischer Tail und 1 % EMOS-Backoff. Fehlende vollständige Kurven liefern die Referenz exakt.

Die unabhängige Standardbibliothek-Inferenz reproduzierte alle 868 gespeicherten Referenz- und Kandidaten-Verteilungen mit maximaler absoluter Abweichung 3,33e-16. Vier archivierte Testfälle sichern beide TAF-Zweige und beide Kurven-Verfügbarkeiten. Parameterdatei: SHA256 `2a99c69a586032b829829eb7c53b9c5a21a6e161b66ca4eb68f5b2e8e0038ced`.

## Zeit und neue Quellenklasse

Checkpoint ist 16:00 Europe/Madrid, mit korrekter Sommerzeit. Erfasst wird das erste gültige Datenpaket zwischen 15:50 und strikt vor 16:00. Auch die Berechnung muss vor 16:00 abgeschlossen sein. Die tatsächliche Erfassungszeit steht in der App; dies ist ein vor dem Checkpoint eingefrorener Shadow, kein fortlaufender Nowcast. Nachträgliche Erfassung oder Auffüllen verpasster Tage ist ausgeschlossen.

Grund: Die produktive Beobachtungstabelle besitzt keine individuelle historische Empfangszeit; Stundenkurven werden teilweise unter Modellzyklen aktualisiert. Ein späterer Datenbankabruf könnte den damaligen Kenntnisstand nicht beweisen. Der neue Ablauf liest eine konsistente, schreibgeschützte Datenbanktransaktion noch vor dem Cutoff und archiviert die tatsächlich gelesenen Eingaben. Die fünfminütige METAR-Verzögerung und 45-Minuten-Altersgrenze der eingefrorenen Methode bleiben erhalten.

Sechs unveränderte gemeinsame Modelle; jeweils jüngster gespeicherter Tageslauf, keine Auswahl eines bequemeren älteren Laufs. Empfang höchstens sechs Stunden, Lauf höchstens 48 Stunden alt. Kurven nur bei gleicher gespeicherter Lauf-ID, vollständigem lokalen Stundentag und identischem Tagesmaximum. Beobachtungszeit wird zwischen benachbarten Stunden interpoliert. Die neue Kurvenvariable ist derselbe Mittelwert der sechs verbleibenden Anstiege über das bekannte METAR-Maximum. Fehlende oder abweichende Kurven werden mit Grund als exakter Referenz-Fallback gespeichert.

Die drei bestehenden Referenz-Merkmale für Restanstieg und Strahlung stammen aus dem letzten bereits gespeicherten Champion-Merkmalsbild; dessen Zeit wird mitgespeichert. Die übrigen Merkmale werden mit den bisherigen Formeln neu aus dem eingefrorenen Paket berechnet. Fehlende Merkmale durchlaufen ausschließlich den eingefrorenen Imputer. TAF: jüngstes damals empfangenes Bulletin; ohne Zieltag-TX kein Rückgriff auf ein älteres bequemeres TX-Bulletin.

Diese neue Quellenklasse heißt `live_point_in_time_composite_v1`. Sie ist unabhängig vom historischen Fixed-Lead-Replay zu evaluieren. Die gespeicherte Lauf-ID beweist nicht, dass getrennte Provider-Antworten einen zertifizierten einheitlichen initialisierten Modelllauf abbilden. Die historischen Verbesserungen werden deshalb nicht als bereits nachgewiesene Live-Verbesserung ausgegeben.

## Getrennter Betrieb und Speicherung

Neuer Workflow: `15 - Madrid v11 prospective Late Shadow`. Vorhandene Collector-, Closeout-, Worker- und Trading-Workflows bleiben unverändert. Er nutzt deren Abschlussereignis lediglich als zusätzlichen Trigger. Fallback-Crons decken beide UTC-Offsets ab. Jeder Lauf prüft die tatsächliche lokale Uhrzeit erneut; verspätete Trigger erzeugen keine Prognose für verpasste Cutoffs. GitHub-Scheduling kann Termine verpassen; das wird als fehlender Tag sichtbar.

Produktionsdaten werden ausschließlich über eine `REPEATABLE READ, READ ONLY`-Transaktion gelesen. Der Modus wird vor dem ersten Quellenabruf mit `SHOW transaction_read_only` geprüft; das Zeitlimit gilt mittels `SET LOCAL` nur für diese Transaktion. So sind keine von Neons Pooler möglicherweise abgelehnten Startup-Optionen oder weiterwirkenden Session-Einstellungen nötig. Begrenzte SELECT-Abfragen, abschließender Rollback; kein Import der DB-Initialisierung. Auf Push erfolgt nur ein Lese-Audit. Es erzeugt keine Prognose und startet kein Backfill.

Entscheidungen mit Quellenpaket, Parameter-/Input-/Record-Hash und Run-ID liegen öffentlich unter `decisions/YYYY-MM-DD.json` auf dem separaten Branch `madrid-v11-shadow-data`. Erster erfolgreicher Datensatz gewinnt; vorhandene Dateien werden nicht verändert. Outcomes liegen separat unter `outcomes/`. Das neu erzeugte `journal.json` enthält dieselben Entscheidungen und getrennte Zusammenfassungen. Kein Schreiben oder Einchecken von Datenbankdateien oder Zugangsdaten. Die App liest das Journal mit kurzem Cache und zeigt ausschließlich das genaue Zieldatum in den vorhandenen Tabellen. Bei Netz-/Validierungsfehlern bleibt die Zelle leer und die Einschränkung sichtbar.

## Auswertung

Ab 06:00 Madrid am Folgetag wird ausschließlich ein akzeptiertes `stored-metar-station`-Tagesmaximum verwendet; keine Reanalyse, AEMET-Rundung, Marktpreise oder modellierten Actuals. Fehlt dieses Actual, bleibt der Tag unbewertet. Vergleich: eingefrorene Referenz sowie ein eindeutig gespeicherter ursprünglicher Champion des gleichen 16-Uhr-Checkpoints; rekonstruierte oder mehrdeutige Champions werden nicht gepaart. Fehlt Champion bei Outcome-Erfassung, bleibt seine Paarung explizit fehlend.

Metriken: Top-1/Top-2, MAE, Bias, Brier, Log Loss, 80%-Intervall-Abdeckung, Top-Label-Reliability und Calibration Error. Champion-Vergleiche erhalten zusätzlich dieselbe gepaarte Tagesmaske. Kurven-Tage und reine Fallback-Tage werden vollständig getrennt gezählt und ausgewertet. Kein OOS-Zähler aus historischen Replays; kein automatisches Bestehen nach 30 Tagen und keine automatische Promotion oder Ausführung.

Erster möglicher neuer Termin nach dieser Veröffentlichung: 24. November 2026. Der bereits vergangene 23. November wird nicht nachträglich als live erfasst.
