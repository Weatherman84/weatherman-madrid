# Daily-Analysis-Automation – Abrufvertrag

## Abrufreihenfolge

1. GitHub Pages: https://weatherman84.github.io/weatherman-madrid/daily-analysis-latest.json
2. Cloudflare: https://weatherman-madrid-scheduler.stefan-fleck.workers.dev/daily-analysis-latest.json
3. Bei veraltetem latest die datierte Datei daily-analysis/YYYY-MM-DD.json
   auf beiden Hosts prüfen.
4. Über den GitHub-Connector die neuesten Closeout-/Export-Workflow-Runs
   und deren Artefakte lesen. Das Artefakt madrid-daily-analysis-export
   enthält den validierten Export und seinen SHA-256-Publikationsnachweis.
   Nur dieses kleine Artefakt laden; keine Repository-Archive.
   Bis zur ersten Veröffentlichung dieses Artefakts keinen Erfolg voraussetzen.

Keine Raw-URLs auf main oder gh-pages verwenden: Veröffentlichung erfolgt
als Pages-Artefakt, nicht als Repository-Datei.
Bei Mirror-Fehlern die funktionierende Primärveröffentlichung separat ausweisen.

## Pflichtvalidierung

Vor jeder Analyse müssen gelten:

- airport == "LEMD"
- classification == "READ-ONLY DAILY ANALYSIS EXPORT"
- contains_credentials == false
- writes_production_database == false
- research_only == true
- generated_at plausibel aktuell
- erwarteter letzter finaler Actual-Tag aus stored-metar-station vorhanden
- Beim Artefakt Dateigröße und SHA-256 gegen Publikationsnachweis prüfen

Stored METAR, AEMET physical Tmax und Market Resolution bleiben getrennt.

## Fehlerverhalten

Alle verfügbaren Fallbacks versuchen; genaue Zugriffssperren nennen.
Ein Tool-Zugriffsfehler beweist keinen App- oder Exportausfall.
Keine Neon-Abfrage, Zugangsdaten, Backfills oder Ersatz-Wetterquellen.
Bei vollständigem Scheitern keine Werte erfinden; Aufgabe aktiviert lassen.
