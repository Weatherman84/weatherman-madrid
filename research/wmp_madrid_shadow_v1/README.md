# WMP Madrid Shadow v1 – technische Entscheidungsvorlage

Stand: 23. November 2026. **Offline-Research-Prototyp. Kein aktiver Shadow in der App, kein neuer Zeitplan, keine Modell-Promotion.**

## Ergebnis

Die vorhandene Weatherman-Infrastruktur kann einen zusätzlichen WMP-Vergleichskandidaten tragen. Der vollständige numerische Pfad wurde mit aktuellen, archivierten API-Antworten für Madrid ausgeführt: 143 echte Ensemble-Member, 20 Jahre Grid-Historie, modellbasierter Regimeinput und echte LEMD-METAR-Werte. Vier öffentliche HTTP-Abfragen, rund 0,2 MB normalisiertes Eingangspaket, keine Datenbankabfrage. Die genaue Laufzeit und Paketgrößen stehen in `evidence/technical-probe-2026-11-23.json`.

Die Historie reicht jedoch noch nicht für einen fairen Vergleich dieses vollständigen Modells: Für **0 von 434** vorhandenen Madrid-Vergleichstagen liegen passende damalige WMP-Member-Pakete vor. Die vorhandenen sechs deterministischen Modelle sind kein Ersatz für die vier EPS-Produkte. Historische Skillmetriken werden deshalb nicht erfunden; das Ergebnisfeld bleibt `null`. Ein weiterer Live-Kandidat ist mit diesem PR noch nicht freigegeben oder aktiviert.

## Was genau nachgebaut wurde

Upstream ist unverändert bei Commit [`cc01f6f228250138a9300e20d3cdc791675bbb96`](https://github.com/portrara/weather-market-predictor/tree/cc01f6f228250138a9300e20d3cdc791675bbb96), am 23.11.2026 erneut gegen den aktuellen GitHub-Stand geprüft. Die benötigten mathematischen Module liegen byte-identisch unter `src/weatherman/research_wmp/upstream`; deren SHA-256 und MIT-Lizenz sind enthalten. Keine fremden Netzwerk-, Cache-, Kalshi- oder LLM-Adapter werden in die App importiert.

Fester Modus ist die allgemeine Stadt-Auswahl mit expliziten Madrid-Barajas-Koordinaten 40,466 / −3,555. Offset 0 °C, Spread Inflation 1,0, History 20 Jahre, Seed 7; keine automatische US-Kalshi-/Meteostat-Stationskalibrierung. Das entspricht den manuellen Standardparametern der externen App. Andere Benutzereinstellungen oder deren aktuelle Bildschirmausgabe werden nicht behauptet.

Die numerische Kette enthält dieselbe harmonische Regression mit drei Harmonischen und Trend, saisonale empirische Residuen, medianen Regimeversatz, Pooling der echten Member, 20.000 Kernel-Ziehungen mit 0,6 °C und 60.000 finale Monte-Carlo-Werte. Am selben Tag beträgt das implementierte Ensemblegewicht 95 %. Die Familien erhalten ihre Gewichte über die Memberzahl, ohne neue Gewichtungsoptimierung.

| Produkt im aktuellen Paket | Member |
|---|---:|
| NCEP GEFS 0,25° | 31 |
| ECMWF IFS 0,25° Ensemble | 51 |
| ICON Seamless EPS | 40 |
| GEM Global Ensemble | 21 |

Fehlt eine Familie, ein Member oder dessen Zielwert, gibt es keine Prognose. Insbesondere kein stiller Ersatz durch deterministische Maxima oder reine Klimatologie.

### Zwei ausdrücklich dokumentierte Madrid-Anpassungen

Der Kandidat heißt **`wmp_madrid_metar_floor_v1`**, nicht „unveränderte externe App“:

1. Die Tagesuntergrenze wird aus tatsächlich gemeldeten LEMD-METAR-Temperaturen mit Empfangszeit, fünf Minuten Mindestverzögerung und höchstens 45 Minuten Alter gebildet. Die externe Funktion `observed_high_today()` verwendet dagegen `temperature_2m` aus der normalen Forecast-API. Eine modellierte Temperatur wird hier nicht als Stationsmessung verwendet.
2. Die gesamte Verteilung wird in die Weatherman-kompatiblen ganzzahligen Buckets `[k−0,5; k+0,5)` überführt. Keine Beschränkung auf acht automatische Anzeige-Bins und kein Abschneiden von Tail-Masse.

Zum Nachweis wird zusätzlich die unveränderte Grid-Mathematik **ohne** Tagesuntergrenze ausgegeben. Ein unabhängiger Aufruf des ursprünglichen `predict.py`, mit denselben eingefrorenen API-Paketen anstelle des Netzwerks, reproduzierte beide entsprechenden Verteilungen exakt: maximale Bucket-Abweichung **0,0**. Beim zweiten Vergleich wurde dem ursprünglichen Floor-Aufruf ausdrücklich derselbe METAR-Wert zugeführt. Das beweist numerische Reproduktion der Berechnung, keine Wetterprognosegüte oder Überlegenheit gegenüber Champion/v11.

GEV und LLM liefern in der Originalpipeline nur Kontext/Kommentar und verändern diese Wahrscheinlichkeiten nicht. Sie werden ebenso wie Marktpreise, Kelly und Trading-Signale nicht benötigt.

## Daten- und Zeitgrenzen

Das Paket vom 23.11. wurde **nach** dem 16-Uhr-Checkpoint aufgenommen. Es ist ein technischer Diagnosefall, keine frühere 16-Uhr-Prognose und kein prospektiver OOS-Tag. Die Aufzeichnung behält die tatsächlichen Empfangszeiten; die Auswertung kann dies nicht durch Umbenennen ändern.

Die Grid-Historie wird wie im Original ohne fest gewähltes `models=era5` angefragt. Sie ist deshalb keine Zusicherung eines ausschließlich aus ERA5 bestehenden Archivs. Der jüngste Regimeinput stammt ebenfalls aus der Forecast-API und wird als modellbasierter Input gespeichert. Historische Reanalyserevisionen und heutige Snapshots dürfen nicht als damals verfügbare Forecasts ausgegeben werden.

Die aktuelle [Open-Meteo-Ensemble-Dokumentation](https://open-meteo.com/en/docs/ensemble-api) nennt nur drei zurückliegende Tage für einzelne Member, daneben längere Aufbewahrung von Mittelwert und Spread. Diese zwei Momente rekonstruieren die vollständige historische Member-Verteilung nicht. Die deterministische [Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api) ersetzt das fehlende EPS-Archiv ebenfalls nicht.

## Anschluss an unsere Infrastruktur

| Baustein | Wiederverwendung / Ergänzung |
|---|---|
| Zeitgeber | Separater GitHub-Actions-Research-Job; gleiche Madrid-Zeit und gleiche Zieltage wie v11. Keine Verlangsamung oder Fehlerkopplung im Champion-Collector. |
| Datenbeschaffung | Neue EPS-Abfrage sowie versionierte Grid-Historie/Regime-Antworten; LEMD-METAR kann später aus derselben schreibgeschützten Quelle wie v11 kommen. |
| Speicherung | Separates WMP-Journal mit Input-/Parameter-/Code-Hashes und vollständigen Eingabepaketen; vorhandenes Git-/Objektablage-Muster. Keine neue SQL-Datenbank oder produktive Tabellen nötig. |
| Frontend | Später eine weitere Spalte in den bestehenden Checkpoint-/Bucket-Tabellen; nur gespeicherte Prognosen laden, kein Klimatologie-Fit oder API-Fan-out beim Seitenaufruf. |
| Vergleich | Champion, v11 und WMP-Madrid auf derselben verfügbaren Tagesmaske und demselben akzeptierten METAR-Tagesmaximum; eigene Quellenklasse und klare Abdeckungszahlen. |
| Zugang | Prototyp nutzt öffentliche Endpunkte. Für einen Dauerbetrieb ist der passende API-Zugang entsprechend den [Open-Meteo-Nutzungsbedingungen](https://open-meteo.com/en/terms) separat zu klären. Kein kostenpflichtiger Zugang wurde bestellt. |

**Nächster notwendiger Schritt:** ein tatsächliches Member-Snapshot-Archiv aufbauen bzw. passende historische Archive beschaffen und deren Run-/Lead-/Verfügbarkeitsgleichheit prüfen. Erst dann ist die im Auftrag verlangte historische Vollmodell-Evaluation möglich. Der vorliegende Code kann auf neue eingefrorene Pakete erneut angewandt werden; eine Numerikprüfung allein öffnet die Aktivierungssperre nicht.

PR #34 und dessen v11-Live-Aktivierung bleiben von dieser Untersuchung getrennt und unverändert. Dieser Research-PR enthält keinen Merge-/Deployment- oder Datenbank-Schreibschritt.

## Reproduktion

Aus dem Repository mit installierten Projektabhängigkeiten:

```bash
PYTHONPATH=src python scripts/capture_wmp_madrid_inputs.py --output /tmp/new-wmp-inputs.json --plan
PYTHONPATH=src python scripts/run_wmp_madrid_shadow.py \
  --snapshot research/wmp_madrid_shadow_v1/evidence/manual-inputs-2026-11-23.json \
  --output /tmp/wmp-madrid-reproduced.json
pytest -q tests/test_wmp_madrid_research.py
```

Ohne `--plan` führt das erste Skript einen einzelnen manuellen Datenabruf aus; es erstellt keinen Zeitplan und berechnet keine Prognose. Bestehende Input- und Ergebnisdateien werden nicht überschrieben. Die Eingangspakete und Artefakte besitzen SHA-256-Prüfungen. Kein Fitting auf späteren METAR-Actuals, keine Parameterwahl anhand von Trading-Ergebnissen.

## Quellen und Attribution

- Originalcode: [Weather Market Predictor](https://github.com/portrara/weather-market-predictor), Copyright © 2026 Krish Yadav, MIT; vollständiger Lizenztext unter dem vendorten Paket.
- Wetterdaten: [Open-Meteo](https://open-meteo.com/), CC BY 4.0, mit Modellen von NOAA/NCEP, ECMWF, DWD und ECCC. Die gespeicherten Antworten sind zu einem reproduzierbaren Eingabepaket normalisiert; Station-Floor und Bucket-Abbildung sind die oben beschriebenen Anpassungen.
- METAR: [NOAA Aviation Weather Center](https://aviationweather.gov/data/api/), explizit LEMD; Quell-Empfangszeit und eigener Abruf bleiben getrennt dokumentiert.
