# WMP-Madrid-Eingabearchiv v1

Vorbereitet am 23.11.2026. Frühester vorgesehener Erfassungstag: 24.11.2026. **Der Workflow wird erst mit dem Merge dieses PRs in den Standard-Branch aktiv.** Der Daten-Branch allein startet keinen Job. PR #34 bleibt getrennt; dessen v11-Job ist für diesen Archiver keine Voraussetzung.

## Zweck und Grenze

Der Archiver baut ein überprüfbares Eingangsdaten-Archiv auf. Er berechnet weder WMP-Wahrscheinlichkeiten noch Empfehlungen, Scores, Trades oder Champion-Ersatzprognosen. Die bestehende Modell-Aktivierungssperre bleibt geschlossen. Auch ein vollständiger Eingabetag zählt deshalb als **0 prospektive Prognose-OOS-Tage**.

Archiviert werden dieselben vier öffentlichen Quellen wie im getesteten Prototyp: die 20-jährige Grid-Historie, der modellbasierte jüngste Regimeinput, alle 143 Member der vier EPS-Produkte und tatsächlich gemeldete LEMD-METAR-Werte. Requests, Einheiten, Stations-/Grid-Koordinaten, tatsächliche Abrufzeiten und normalisierte Quellantworten bleiben mit SHA-256 erhalten. Weder Datenbankzugang noch Provider-Secrets sind nötig. Der bestehende Collector und die produktiven Tabellen bleiben unverändert.

## Zeitvertrag

- Ausschließlich Madrid und Late Live 16:00 Europe/Madrid. UTC-Offsets werden über die tatsächliche lokale Zeitzone aufgelöst.
- Das erste vollständige Paket gewinnt: tatsächlicher Abrufbeginn frühestens 15:50, Empfang und lokale Archivierung strikt vor 16:00. Eine deklarierte Slot-Zeit ersetzt niemals die tatsächliche Uhrzeit.
- GitHub-Cron-Versuche liegen bei 13:50/13:55 und 14:50/14:55 UTC; nur das passende Madrider Zeitfenster löst API-Aufrufe aus. Der erfolgreiche Abschluss des bestehenden Madrid-Collectors dient als zusätzlicher Trigger, ohne dessen Code zu ändern.
- Kommt ein Trigger erst nach dem Zeitfenster zur Ausführung, gibt es keinen nachträglichen Abruf für den Checkpoint. Endet ein bereits gestarteter Abruf zu spät, wird das Paket nur als verspätete Diagnose aufbewahrt.
- Ab 16:10 vermerken Abschlussläufe einen nicht erfassten Tag ausdrücklich als Lücke. Ein abendlicher Fallback deckt ausgefallene frühe Abschlussläufe ab. Eine geschlossene Tageslücke wird nicht nachträglich ersetzt.
- Fehlende Member, falsche Stationen/Einheiten, ungültige Hashes, alte oder widersprüchliche Quellzeitstempel führen zu einem Diagnoseversuch statt einem ausgewählten Checkpoint. Ein neuer tatsächlicher Versuch darf bis 16:00 noch ein vollständiges Paket liefern.

GitHub kann geplante Jobs verzögern oder auslassen. Daher wird keine garantierte tägliche Abdeckung behauptet. Das Journal zeigt ausgewählte Eingabetage, Fehlversuche und tatsächlich festgestellte Lücken getrennt. Tage vor Beginn des Archivs werden nicht als erfasste Historie dargestellt. Die Daten besitzen die Quellenklasse eines tatsächlich erfassten API-Pakets; sie beweisen keinen einheitlichen initialisierten Modelllauf aller Anbieter.

## Unveränderlicher Speicher

Separater öffentlicher Branch: `wmp-madrid-input-archive`.

| Pfad | Inhalt | Änderungsregel |
|---|---|---|
| `bundles/<canonical-input-sha>.json` | Vollständiges oder diagnostisches Eingabepaket | Einmalig; niemals überschreiben |
| `attempts/<run-id>-<run-attempt>.json` | Tatsächlicher Versuch, Eingangshash, Status und Zeiten | Einmalig; Wiederholung derselben ID ohne neuen Abruf |
| `checkpoints/YYYY-MM-DD.json` | Erstes rechtzeitiges, vollständig validiertes Paket des Tages | Einmalig; weitere Läufe ohne API-Aufruf |
| `gaps/YYYY-MM-DD.json` | Nach Ablauf festgestelltes Fehlen eines gültigen Pakets | Einmalig; kein Backfill |
| `journal.json` | Abgeleiteter kompakter Index | Aus vorhandenen unveränderlichen Dateien neu erzeugbar |

Die Eingabeobjekt-ID ist der SHA-256 des kanonischen JSON-Inhalts, nicht der HTTP-Kompression. Quell-Hashes beziehen sich auf normalisierte Datenantworten. Sie erleichtern Reproduktion und zeigen Änderungen; es handelt sich nicht um eine vom Wetterdienst signierte Veröffentlichung. Der eigene Abrufzeitpunkt, die Actions-Run-ID, die tatsächlich ausgecheckte Code-Revision und die Git-Historie bilden die zusätzliche Provenienz.

`write_once` vergleicht vorhandene Bytes und verweigert Änderungen. Vor jedem Lauf werden bestehende Checkpoint-Verweise, Quell- und Objekthashes geprüft. Der Publisher verweigert Änderungen bereits versionierter Dateien in den vier unveränderlichen Verzeichnissen; nur neue Dateien und der Index dürfen gepusht werden. Push-Ziel ist ausdrücklich der Daten-Branch, niemals `main`; es gibt keinen Force-Push. Ein Push-Konflikt scheitert sichtbar statt bestehende Evidenz zu ersetzen.

Ein erfolgreiches Eingangspaket benötigt in der aktuellen Stichprobe etwa 0,2 MB; die Originalantworten bleiben für spätere Prüfungen erhalten. Die eigentliche App lädt dieses Archiv noch nicht. Für die spätere Anzeige kann ein eigener kleiner Forecast-Index verwendet werden, ohne sämtliche Quellpakete im Frontend zu verarbeiten.

## Betrieb und Prüfung

Workflow: `16 - Archive WMP Madrid inputs`. Ein Push der relevanten Dateien in `main` führt zunächst ausschließlich einen **Audit ohne API-Abfragen** aus; manuell ist `audit` ebenfalls die Voreinstellung. `auto` nutzt immer das tatsächliche aktuelle Zeitfenster. Der Workflow besitzt lediglich Repository-Schreibrecht für seinen Publisher und bekommt keine DB- oder Anbieter-Secrets. Laufzeitlimit: fünf Minuten, vier parallele, größenbegrenzte HTTP-Abfragen, keine automatische Retry-Schleife im Provideradapter.

Nach dem Merge sind zu prüfen: grüner anfänglicher Audit-Run und lesbarer Index; anschließend ein vor 16:00 ausgewähltes Paket oder ein erklärter Fehl-/Lückentag. Ein reiner Datenabruf nach dem Checkpoint ist hierfür kein Ersatz. Die Lizenz-/API-Zugangsprüfung für einen späteren produktiven Dauerbetrieb bleibt wie in der Entscheidungsvorlage beschrieben bestehen; dieser Job dient ausschließlich dem Research-Eingabearchiv und löst keine Bestellung aus.

Lokaler Integritätstest ohne Netzwerk oder Datenbank:

```bash
PYTHONPATH=src python scripts/archive_wmp_madrid_inputs.py --store /tmp/wmp-input-audit --mode audit
pytest -q tests/test_wmp_input_archive.py tests/test_wmp_madrid_research.py
```

Die Unit-Tests verschieben ein vorhandenes Paket auf synthetische Testuhren, um Sommerzeit und Grenzfälle zu prüfen. Diese künstlich datierten Fixtures werden niemals als Wetterprognosen oder historische Evidenz veröffentlicht. Die bereits gespeicherten realen Diagnosepakete und ihre ursprünglichen Zeitstempel bleiben unverändert.
