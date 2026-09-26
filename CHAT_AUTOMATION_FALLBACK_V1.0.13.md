# Daily-Analysis-Automation – Fallback-Vertrag v1.0.13

Dieser Vertrag wird erst aktiviert, nachdem der Cloudflare-Endpunkt aus der
ChatGPT-Automationsumgebung tatsächlich geöffnet und validiert wurde.

## Abrufreihenfolge

1. Primär:
   `https://weatherman84.github.io/weatherman-madrid/daily-analysis-latest.json`
2. Bei Sicherheitsblock, Timeout oder HTTP-Fehler sekundär:
   `<AEMET_PUBLIC_BASE_URL>/daily-analysis-latest.json`
3. Ist `latest` veraltet, für den erwarteten Madrid-Tag gezielt:
   `<AEMET_PUBLIC_BASE_URL>/daily-analysis/YYYY-MM-DD.json`

## Pflichtvalidierung

Vor jeder Analyse müssen gelten:

- `airport == "LEMD"`
- `classification == "READ-ONLY DAILY ANALYSIS EXPORT"`
- `contains_credentials == false`
- `writes_production_database == false`
- `research_only == true`
- `generated_at` vorhanden und plausibel aktuell
- erwarteter letzter finaler Actual-Tag in `actuals` vorhanden

Stored METAR, AEMET physical Tmax und Market Resolution bleiben getrennt.

## Fehlerverhalten

- Einen einzelnen Abruffehler melden, aber die tägliche Aufgabe nicht deaktivieren.
- Bei Fehler des Primärpfads immer den Sekundärpfad versuchen.
- Keine direkte Neon-Abfrage und keine andere Wetterquelle als Ersatz öffnen.
- Bei Fehler beider Pfade keine Werte erfinden.
