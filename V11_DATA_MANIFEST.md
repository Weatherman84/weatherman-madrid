# V11 Datenmanifest

Stand: 22.11.2026. Offline, research_only=true, automatic_promotion=false, automatic_execution=false.

## Tatsächlich ausgewertete Daten

| Bestand | Tage / Zeilen | Evidenz | Verwendung |
|---|---:|---|---|
| Deep Seasonal Replay, 01.11.2024–08.10.2026 | 685 akzeptierte Tage / 2.740 Checkpoints | historical_reduced_fidelity | langes Discovery-/Kalibrierungs-/Validation-Experiment |
| Historical Replay v0.3, April–Oktober 2026 | 764 ursprüngliche Checkpoints, 735 ursprünglich evaluierbar | Initialisierung erhalten, Veröffentlichung geschätzt | separater Acht-Modell-Vergleich |
| Daily-Exporte, 11.10.–20.11.2026 | 161 Checkpoints / 41 Kalendertage | zeitgerecht, rekonstruiert, unbekannt getrennt | retrospektive Diagnose; kein v11-OOS |
| Six-Airport Benchmark | 6 Airports, fertige historische Studien | separate Benchmark-Artefakte | Kontextinventar; keine neu gefittete Multi-Airport-Engine |

Kompakte Hauptkohorte: 2901 Zeilen. Originale Artefakte bleiben unverändert. Quellen-SHA-256: `artifacts/input_manifest.json`; Modell-/Variablenabdeckung: `artifacts/extended_manifest.json`.

## Hauptkohorte nach Evidenzklasse

| Klasse | Checkpoints |
|---|---:|
| historical_reduced_fidelity | 2740 |
| reconstructed | 3 |
| scheduled_causal_production | 158 |

## Modellabdeckung in der Hauptkohorte

| Modell | Checkpoints | Erster Tag | Letzter Tag |
|---|---:|---|---|
| ecmwf_ifs025 | 2901 | 2024-11-01 | 2026-11-20 |
| gfs_global | 2901 | 2024-11-01 | 2026-11-20 |
| icon_eu | 2900 | 2024-11-01 | 2026-11-20 |
| icon_global | 2901 | 2024-11-01 | 2026-11-20 |
| meteoblue | 161 | 2026-10-11 | 2026-11-20 |
| meteofrance_arome_france | 159 | 2026-10-11 | 2026-11-20 |
| meteofrance_arome_france_hd | 159 | 2026-10-11 | 2026-11-20 |
| meteofrance_arpege_europe | 2897 | 2024-11-01 | 2026-11-20 |
| ukmo_global_deterministic_10km | 2689 | 2024-11-01 | 2026-11-20 |

Die Zahlen beschreiben verfügbare Datensätze, keine unabhängigen Ensemble-Mitglieder. AROME und AROME HD werden zusätzlich im separaten Experiment geprüft. Meteoblue hat keinen langen historischen Trainingsbestand; kein fairer historischer Skill-Nachweis dafür möglich.

## Verfügbarkeit und zeitliche Grenzen

Deep: feste Previous-Run-Leads, keine zertifizierten Einzelrun-Publikationszeiten. METAR-Verfügbarkeit: historische Schätzung Beobachtung +5 Minuten. Kein `historical_exact_run`- oder Produktions-OOS-Nachweis.

Daily: unabhängige Modellinputs erfordern initialisiert ≤ veröffentlicht ≤ abgerufen ≤ Cutoff, höchstens 48 Stunden seit Initialisierung. Deshalb können sie älter sein als die vom ursprünglichen Collector nach dem Cutoff abgerufenen Inputs. Dieser Versorgungseffekt wird nicht als Prognosefehler des Wettermodells ausgegeben. Modellalter im Featurevektor meint Minuten seit Verfügbarkeit, nicht seit Initialisierung.

Daily-Observationen haben Beobachtungszeit, aber keinen verifizierten historischen Empfangszeitpunkt. Der Vergleich bleibt diagnostisch, auch bei ursprünglich scheduled-causal gespeicherten Champion-Forecasts. Diese Klasse zertifiziert nicht den nachträglich gerechneten v11-Kandidaten.

Nur final akzeptierte Stored-METAR-Tagesmaxima sind Trainingslabels. AEMET physical Tmax und offizielle WU-Marktauflösung werden nicht als Ersatz verwendet. Labelverfügbarkeit ist historisch Tagesende +10 Minuten geschätzt und wird vor Fit geprüft.

## Featureabdeckung

| Feature | Deep nicht-null | Daily nicht-null |
|---|---:|---:|
| observed_max | 2055 | 120 |
| gap_to_max | 2055 | 120 |
| plateau_minutes | 2055 | 120 |
| heating30 | 2054 | 120 |
| heating60 | 2054 | 120 |
| heating120 | 2055 | 120 |
| observation_age | 2055 | 120 |
| wind_speed | 2055 | 120 |
| wind_sin | 1727 | 101 |
| wind_cos | 1727 | 101 |
| wind_change | 2055 | 120 |
| dewpoint_change | 2055 | 120 |
| cloud_cover | 2052 | 120 |
| remaining_model_rise | 2740 | 161 |
| model_radiation | 2740 | 161 |
| future_radiation | 2740 | 161 |
| prior_minus_observed | 2055 | 120 |
| model_spread | 2740 | 161 |
| season_sin | 2740 | 161 |
| season_cos | 2740 | 161 |
| local_hour | 2740 | 161 |
| model_age | 2740 | 161 |
| model_count | 2740 | 161 |

Fehlende Merkmale werden ausschließlich mit Discovery-Medianen und Missingness-Indikatoren behandelt. Nicht verfügbar: durchgängig gemessene Strahlung, vollständige vertikale Profile und historisch verifizierte vollständige Ensemble-Mitglieder. Modellstrahlung ist keine Messung.

## Historische APIs und externe Referenzen

Am 22.11.2026 wurden die offiziellen Dokumentationen und alle fünf im Handoff genannten öffentlichen Projektseiten geprüft. README-Selbstauskünfte sind keine unabhängig geprüften Erfolgsnachweise; externer Code wurde nicht übernommen.

- [Open-Meteo Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api): gestitchte Zeitreihe von Anfangsstunden. Single Runs bewahrt einzelne Läufe; laut Dokumentation ECMWF IFS HRES ab März 2024, andere Modelle ab 02.04.2026. Initialisierung allein belegt keine damalige Veröffentlichungszeit.
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api): echte Mitgliederprodukte und Past-Days-Funktion; kein Nachweis eines lückenlosen mehrjährigen Mitgliederarchivs im gelieferten Datenbestand. Keine erfundenen 100 unabhängigen Mitglieder aus sechs deterministischen Modellen.
- [Weather Market Predictor](https://github.com/portrara/weather-market-predictor): probabilistische Priors, Stationsfehler und Spread prüfen; Monte-Carlo-Samples schaffen keine zusätzliche unabhängige Evidenz.
- [Command Center](https://github.com/testedmedia/polymarket-weather-command-center): laufendes Maximum, Hold-Raten und exakte Resolution prüfen; keine pauschalen Solar-/Uhrzeit-Locks übernehmen.
- [Weather Edge](https://github.com/jattree/weather-edge): negativer Postmortem unterstreicht Marktbenchmark und Ausführungskosten; kein Erfolg allein aus Wetter-Kalibrierung ableiten.
- [Augustforge](https://github.com/Augustforge/weather-prediction-market-bot): getrennte Komponenten und unveränderliche Shadow-Aufnahmen als Architekturhinweise, kein nachgewiesener Echtgeld-Edge.
- [Weather Trading Bot](https://github.com/lukasjagnesak/Weather-Trading-Bot): EMOS und Kosten-/Risikologik als Referenzen; keine veröffentlichten Performancebehauptungen als Freigabenachweis.

## Bewusst offene Datenlücken

Keine neuen öffentlichen API-Backfills ausgelöst; keine Produktions-DB gelesen. Die nachgelagerte Versorgung / Mitgliederarchivierung ist nicht deployt. Nicht vorhandene Original-Handoffs, insbesondere 18.–20.11., werden durch Raw-Exporte ergänzt, aber nicht als gelesene Originaltexte dargestellt. Der 23.10. hatte ursprünglich keinen Abschluss-Handoff; jetzt vorhandene Rohdaten ändern diesen historischen Dokumentationsstatus nicht.
