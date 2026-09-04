# Project Handoff – Weatherman Madrid v1.0.7

## Basis

- Ausgangscode: öffentliches Weatherman v10.7.10, geprüfter Quell-HEAD `8b194c2`.
- Produktiver Engine-Stand: v10.7.11; nur die Modell-Freshness-Auswahl wurde geändert.
- Neuer Produktscope: ausschließlich LEMD / Madrid-Barajas.
- Neue App-Version: v1.0.7.

## AEMET Physical Observations und Ground Truth v1.0.7

- Cloudflare ruft ausschließlich AEMET Madrid Aeropuerto `3129` alle zehn Minuten ab.
- Zwei kleine KV-Werte halten Live-Status und heutige Kurve; abgeschlossene Tage werden
  als `archive/aemet/YYYY/MM/DD.json.gz` komprimiert archiviert.
- Das Streamlit-Cockpit prüft den Status alle fünf Minuten in einem isolierten Fragment.
  Champion, Modelle, TAF und Neon werden dadurch nicht neu geladen.
- AEMET-Dezimaltemperatur, Physical Tmax und LEMD-METAR-Punkte werden getrennt gezeigt.
- Export-Schema 1.3 führt AEMET Physical Observations getrennt. Ein
  `market_resolution_actual` bleibt leer, bis Polymarket-Quelle und Rundungsregel
  bestätigt sind.
- `stored_metar_max`, `aemet_physical_tmax` und `market_resolution_actual` sind drei
  getrennte Evaluation Targets und dürfen nicht in einer Scorecard vermischt werden.
- `daily_max_series_gap_c` ist eine Differenz zwischen Datenreihen und kein erlernter
  Sensor-Bias; sie kann Standort-, Sensor-, Zeit- und Verarbeitungsunterschiede bündeln.
- Zeitnahe AEMET-/METAR-Paare werden mit ihrem Zeitabstand exportiert, bleiben aber
  ebenfalls reine Reihenvergleiche.

## Stall- und METAR-Persistence-Shadow

- `physical_stall_shadow` beschreibt ausschließlich, ob die physische AEMET-Reihe
  steigt, flach ist oder bereits zurückgeht.
- `metar_bucket_persistence_shadow` trennt davon die Frage, ob der bisher höchste
  ganzzahlige METAR-Wert bestehen bleiben könnte.
- Beide Signale sind unkalibriert, liefern bis zu ausreichender sequenzieller
  OOS-Evidenz keine Prozentwahrscheinlichkeit und greifen nicht in Champion, Buckets,
  Peak-Lock, Regime oder Marktentscheidung ein.
- AEMET-Ausfälle sind isoliert und können Collector oder Champion nicht stoppen.
- Keine Forecast-, Bias-, Weight-, Regime-, Lock- oder Promotion-Änderung.

## Transferarme Hybrid-Kadenz v1.0.5

- Cloudflare dispatcht im aktiven Fenster alle 30 Minuten einen leichten
  Aviation-Lauf für METAR, TAF und Actual-Reparatur.
- Nur D−1 @20:00, D0 @09:00, First Live @12:00 und Late Live @16:00 führen
  automatisch einen vollständigen Modell-, Champion- und Regimelauf aus.
- Der 21:15-LT-Tagesabschluss bleibt ein eigener vollständiger Post-Peak-/Actual-Lauf
  und veröffentlicht anschließend den Analyseexport.
- Pipeline-Health ordnet verspätete geplante Läufe innerhalb von 20 Minuten
  triggerbasiert dem kanonischen Sollslot zu. Damit erfüllt der GitHub-Fallback um
  21:22 LT den 21:15-LT-Closeout, ohne manuelle Runs als Coverage zu zählen.
- Der Export nennt cadence-gültige Modelle `usable` und trennt
  `current_latest_run`, `awaiting_next_run`, `missing_expected_run` und `hard_stale`.
  Die interne Bestands-Spalte `fresh_model_count` bleibt nur aus Datenbankkompatibilität
  bestehen und wird nicht mehr mit dieser missverständlichen Bezeichnung exportiert.
- Jeder manuelle Streamlit-Refresh speichert Forecast Ladder, Buckets, Modellprovenienz,
  Forecast Drivers, Regimes, Champion/Challenger und Polymarket als `Manual Live`.
- `Manual Live` ist kausal OOS, bleibt aber eine eigene nicht standardisierte Kohorte;
  mehrere Klicks können den sequenziellen 30-Tage-Zähler nicht aufblasen.
- Bekannte Open-Meteo-Modellzyklen dienen als persistenter Speicherschlüssel. Ein
  identischer Lauf wird nicht pro Fetch-Zeitpunkt vollständig dupliziert.
- Cockpit-Lesehistorie ist auf 120 Tage begrenzt; Replay und Export bleiben getrennt.
- Engine v10.7.11, Formeln, Gewichte, Biases, Regimes und Locks sind unverändert.

## Modellabhängige Freshness v1.0.4

- Je Modell wird der neueste am Checkpoint kausal verfügbare Lauf verwendet.
- ECMWF, GFS, ICON Global, UKMO und ARPEGE folgen einer 6-Stunden-Kadenz.
- ICON-EU und AROME/AROME-HD folgen einer 3-Stunden-Kadenz.
- Ein Lauf bleibt während des normalen Veröffentlichungsfensters verwendbar.
- Sobald ein neuer Lauf über die Toleranz hinaus fehlt, wird der alte Lauf
  ausgeschlossen; mehrere verpasste Zyklen ergeben `hard_stale`.
- Die Checkpoint-Provenienz speichert Status, Referenzzeit, Kadenz, Toleranz,
  nächsten erwarteten Lauf und Zahl verpasster Updates.
- Sequenzielle Promotionsevidenz beginnt für v10.7.11 am 31. August 2026 neu; die
  bisherigen v10.7.10-Tage werden nicht angerechnet.
- Forecastformeln, Biases, Gewichte, Regime, Locks und Promotion-Gates sind unverändert.

## Scheduler- und Export-P0-Fix v1.0.3

- Cloudflare Cron Triggers erzeugten bis v1.0.4 die primären 15-Minuten-Dispatches.
- Jeder externe Aufruf übergibt seinen unveränderlichen UTC-`scheduled_slot`.
- PostgreSQL Advisory Locks und vorhandene CollectionRun-Daten verhindern eine
  Doppelverarbeitung desselben Airport-/Slot-Paars.
- GitHub-Cron läuft nur noch stündlich als Safety Net.
- Ein eigener 21:15-LT-Closeout sammelt den letzten METAR-/Actual-/Post-Peak-Stand und
  ruft anschließend den Read-only-Export auf.
- Der Export kann nicht mehr anhand der tatsächlichen Runner-Uhrzeit übersprungen werden.
- Build und veröffentlichte Pages-Datei müssen denselben aktuellen
  `generated_at`-Zeitpunkt besitzen.
- Pipeline-Health, Strahlung und 850-hPa-Temperatur werden exportiert.
- Keine Forecast-Engine-, Bias-, Weight-, Regime- oder Lock-Änderung.

## Technischer P0-Fix v1.0.2

- Live-Forecasts, Stundenwerte, METARs und Marktpreise werden unter PostgreSQL/Neon
  in konfliktgesicherten Batches von höchstens 500 Zeilen gespeichert.
- SQLite behält den bisherigen, getesteten Speicherpfad.
- Das Cockpit trennt Provider-, Neon- und Checkpoint-Laufzeit.
- Der Checkpoint-Marktaufruf ist auf einen Versuch und sieben Sekunden begrenzt.
- Keine Forecast-Engine-, Bias-, Weight-, Regime- oder Lock-Änderung.

## Architekturentscheidung

- Eigenes öffentliches GitHub-Repository.
- Eigene Streamlit-App.
- Neon-PostgreSQL für Produktion.
- Zweites, eigenständiges Neon-Projekt für Replay.
- Keine produktive SQLite-Datei und kein DB-Commit/Push durch Collector.
- Alte App bleibt unverändert als Rollback.

## Feste Checkpoints

1. D−1 Evening @20:00 LT.
2. D0 Morning @09:00 LT.
3. First Live @12:00 LT.
4. Late Live @16:00 LT.

Meteoblue wird höchstens einmal pro Checkpoint-Fenster versucht, maximal viermal pro
lokalem Kalendertag. Eine persistente `provider_calls`-Zeile und PostgreSQL-Sperren
verhindern Doppelverbrauch zwischen GitHub und Streamlit.

## Replay-Schutz

- Production-Verbindung wird in eine Read-only-Transaktion gesetzt und zurückgerollt.
- Ergebnisse werden ausschließlich unter `replay_lab` im Replay-Projekt gespeichert.
- Evidenzklassen bleiben `historical-causal`, `reconstructed-research`, `unavailable`.
- Keine Promotion und keine produktive Konfigurationsänderung.

Workflow 5 ist ein Pilot auf gespeicherten Production-Snapshots, kein vollständiger
externer Archive-Replay. Der spätere 360-Tage-Archive-Replay bleibt ein eigenes Vorhaben.

## Daily Analysis Export

- Workflow 6 liest Production explizit read-only und rollt die Transaktion zurück.
- Der Export ist auf LEMD und sieben Analysetage begrenzt.
- Er enthält Checkpoints, Forecast Drivers, Regime-/Adjustment-Impacts, Varianten,
  Actuals, TAF/METAR-Provenienz und Collector-Abdeckung.
- Credentials und interne IDs werden nicht veröffentlicht; Collector-Referenzen sind
  nur kurze Hashwerte.
- GitHub Pages stellt eine stabile lesbare URL für die tägliche 21:30-Analyse bereit.

## Acceptance

- alle Tests und Ruff müssen grün sein;
- Workflow-Dateien müssen syntaktisch valide sein;
- Testmigration muss ausschließlich LEMD übernehmen;
- Release-Paket muss unter 100 Dateien bleiben;
- kein DB-, Archiv-, Cache- oder Secret-File im Release;
- Replay und Production dürfen nicht dieselbe Datenbankidentität besitzen.

## Nach dem Deployment

- zwei bis drei Tage Parallelvergleich mit der alten App;
- Checkpoint-Abdeckung, Reliability-N und Provider-Calls prüfen;
- Neon Usage nach drei und sieben Tagen kontrollieren;
- erst danach über Abschaltung alter Collector-Workflows entscheiden.
