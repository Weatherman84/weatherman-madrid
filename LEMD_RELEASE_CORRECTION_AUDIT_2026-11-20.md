# LEMD Release-Korrektur – Evidenz-Audit

Stand der Eingangsdaten: 20.11.2026  
Umfang: Versorgung und Diagnostik; keine Änderung der Forecast- oder Wettlogik.

`research_only=true`  
`automatic_promotion=false`

## Morning D0@09:00

### Belegter Software-Fehlerpfad

1. Vor dieser Korrektur war der automatische Lauf um 08:xx Madrid nur ein
   Aviation-Lauf. Der erste erzwungene Modellabruf für Morning erfolgte damit
   erst nach dem Informations-Cutoff 09:00.
2. Schlägt dieser Abruf fehl und liegt kein ausreichend aktueller, bereits vor
   dem Cutoff gespeicherter Lauf vor, liefert `_checkpoint_provenance` den
   Status `unavailable`.
3. `collect_research_checkpoints` erhöhte daraufhin lediglich
   `checkpoints_missing_inputs` und setzte die Verarbeitung mit `continue` fort.
   Es wurde weder ein Forecast-Snapshot noch ein dauerhafter Nachweis des
   fehlenden Checkpoints geschrieben.
4. Der umgebende Collector wurde trotzdem mit `overall_status=success`
   abgeschlossen. Daher konnte ein erfolgreicher Collector-Lauf einen fehlenden
   Morning-Forecast verdecken.

Der Prüfbericht belegt Open-Meteo-Quellenfehler für 17.–19.11. Die ursprünglichen
Job-Log-Texte und Run-IDs liegen weder im Repository noch im übergebenen Bericht.
Deshalb kann aus den vorhandenen Artefakten nicht belastbar unterschieden werden,
ob der einzelne Abruf jeweils an Timeout, HTTP-Status, Transport oder Parser
scheiterte. Dieser fehlende Provider-Nachweis wird nicht nachträglich erfunden.

### Korrektur

- Der automatische 08:xx-Lauf Madrid ist nun ein Pre-Cutoff-Modelllauf. Seine
  Inputs werden vor 09:00 gespeichert und stehen dem Morning-Checkpoint bei
  einem späteren Providerfehler als kausaler Fallback zur Verfügung.
- Nach dem Cutoff verfügbare Inputs bleiben ausgeschlossen.
- Jeder fällige Checkpoint erzeugt im Collector-Journal einen Status
  `written`, `missing`, `reconstructed` oder `failed` mit Cutoff, Speicherzeit,
  Modellzahlen, Providerstatus und konkretem Grund.
- Eine aktuelle Morning-Lücke setzt den Collector-Lauf über den separaten
  Delivery-Guard auf `partial`; der Guard beendet den Workflow mit Exit-Code 2.
  Alte Catch-up-Lücken bleiben sichtbar, lösen
  aber keine wiederholten Workflow-Fehler aus.
- Der Daily-Analysis-Export enthält `checkpoint_delivery_audit` und weist
  partielle Läufe explizit als `has-gaps` aus.

## Analog Late 16:00 – bereinigtes Gate

| Kohorte | Paare | MAE-Gewinn | Hürde | MAE-Gate | Release-Folge |
|---|---:|---:|---:|---|---|
| Veröffentlichte historische Kohorte | 82 | 0,1224795 °C | 0,1200000 °C | PASS | keine automatische Freigabe |
| Nur nachgewiesenen rekonstruierten 16.11. entfernt | 81 | 0,1188178 °C | 0,1200000 °C | FAIL | weiter Shadow |

Aufnahmeprovenienz der 82 Paare: 51 zeitgerecht, 1 rekonstruiert und
30 unbekannt. Unbekannte Evidenz wird weder automatisch akzeptiert noch
verworfen. Der Export erhält deshalb zusätzlich getrennte Gates für:

- zeitgerecht;
- rekonstruiert;
- unbekannt;
- nicht rekonstruiert einschließlich unbekannter Alt-Evidenz.

Der bisherige historische Gate-Status bleibt unverändert dokumentiert. Die
Release-Empfehlung ist davon getrennt und bleibt `CONTINUE_SHADOW`, weil der
bekannte rekonstruierte Fall den PASS/FAIL-Ausgang verändert und 30 ältere
Aufnahmen keine belegte Speicherprovenienz besitzen.

## Post-Peak Upper-Tail

Die zwölf zeitgerechten, verfügbaren Kandidatenfälle bleiben eine
Verfügbarkeitskohorte und keine vollständige sequenzielle Freigabekohorte:

| Modell | Modal-Treffer | MAE | Brier |
|---|---:|---:|---:|
| Champion | 5/12 | 0,826 °C | 0,5655 |
| Post-Peak | 9/12 | 0,592 °C | 0,3228 |

Der Kandidat wird weiter sequenziell geführt. Der späte Wiederanstieg am
17.11. bleibt verpflichtender Gegenfall. Keine Promotion aus diesen zwölf
ausgewählten Fällen.

## Release-Empfehlung

**Noch kein Forecast-Release.**

Vor einer separaten Freigabe von Analog Late müssen mindestens folgende Punkte
erfüllt sein:

1. Die bereinigte, zeitgerechte Kohorte besteht alle unveränderten Gates.
2. Die 30 unbekannten historischen Aufnahmen sind geklärt oder werden als eigene
   Sensitivitätskohorte behandelt; sie dürfen nicht stillschweigend gültig werden.
3. Der reparierte Morning-Pfad liefert neue, zeitgerecht gespeicherte Nachweise
   ohne rückwirkende Inputs.
4. Post-Peak besitzt eine vollständige sequenzielle Bilanz einschließlich der
   Gegenfälle.

Champion-Formel, Gewichte, Biaswerte, TAF-Stufe, Day-/Peak-Lock, geschützte
Baseline und Wettlogik bleiben unverändert.

Die geschützten Engine-Dateien werden durch diese Korrektur bytegenau nicht
verändert. Pre-Cutoff-Steuerung und Delivery-Audit liegen in einem separaten
Guard und im Workflow.
