# Weatherman Opportunity Scanner v0.1-shadow

Separate Research-/Shadow-App für `LEMD`, `EHAM`, `EDDM`, `EPWA`, `LTAC` und `LTFM`.
Sie verändert weder die Madrid Forecast Engine noch Madrid-Daten oder Production-Regeln.

## Unveränderliche Sicherheitsgrenzen

- `research_only=true`
- `automatic_promotion=false`
- `automatic_execution=false`
- keine Production-Neon-Verbindung im Scanner
- keine Backfills vor dem expliziten Aktivierungsdatum
- `$1` nominal je Shadow Trade, kein Echtgeld und kein Kelly-Sizing
- Historical Research Evidence und `sequential_oos_shadow` bleiben getrennt

## Komponenten

| Komponente | Zweck |
|---|---|
| `opportunity_scanner_app.py` | separate Streamlit-App |
| `opportunity_scanner.py` | Rule-Hash, kausale Peak-Zeit, Frozen Rule Matching, Status/Edge/P&L |
| `opportunity_portfolio.py` | Scanner-Zyklus, Portfolio, Funnel, Forward-Metriken |
| `opportunity_store.py` | separate append-only SQLite-Shadow-Persistenz |
| `opportunity_feed.py` | kausaler Live-Feed aus Common Models, METAR und Market Quotes |
| `scripts/build_opportunity_scanner_feed.py` | sechs Airports erfassen, ohne Datenbankzugriff |
| `scripts/run_opportunity_scanner.py` | Feed auswerten und separates Shadow-Journal bauen |
| `opportunity-scanner-worker/` | KV-Publisher für Feed und append-only Journal |
| `.github/workflows/opportunity-scanner-feed.yml` | kausaler Feed-Aufbau manuell oder im kontrollierten Forward-Clock |
| `.github/workflows/opportunity-scanner-shadow.yml` | Shadow-Lauf manuell oder nach erfolgreichem Feed-Lauf |

## Frozen Inputs

Aktiv sind exakt 24 Regeln: 4 HC-A und 20 HC-B. Der kanonische Hash ist:

`b08f9e5be300a46dc82eebd8b5f09d35ae480799ea2ccbd6d5e816b65cbd4420`

12 HC-C-Regeln sind nur lesbar. Rejected Rules sind nicht im aktiven Paket. Die
Common-Model-Menge ist exakt `ecmwf_ifs025`, `gfs_global`, `icon_global`.

## Kompakter Feed-Vertrag

Der Scanner liest keine Forecast-Historie aus Production. Ein separater Publisher
liefert nur aktuelle kausale Snapshots, maximal 60 valide frühere Peak-Zeiten je Airport
und aktuelle Marktquotes. `schema_version` ist `opportunity_scanner_feed_v1` und
`contains_outcomes` muss `false` sein.

Die Forecast-Seite verwendet exakt die drei Common Models bei festem 24-Stunden-Lead
aus Open-Meteo Previous Runs. Bias, inverse-MAE-Gewichte und Sigma stammen aus dem
abgeschlossenen kausalen Benchmark und sind mit Cutoff 8. Oktober 2026 eingefroren.
Die erwartete Peak-Zeit verwendet ausschließlich frühere Tage. Ein verpasstes
Checkpoint-Fenster oder eine unvollständige Common-Model-Menge darf kein unveränderliches
Forward-OOS-Signal erzeugen.

Outcome-Felder werden im Live-Feed abgewiesen. Resolutionen kommen ausschließlich über
ein separates `opportunity_scanner_resolution_feed_v1` und müssen eine offizielle
Settlement-Quelle nennen. Ein METAR-Bucket wird getrennt gespeichert und niemals als
Market Resolution interpretiert.

## Frozen HIGH INTEREST Policy

HIGH INTEREST verlangt HC-A/B, exakten Bucket-Market-Match, Best Ask, Quote-Alter höchstens
30 Minuten, Spread höchstens 0,15, keine Data-Quality-Warnung und positiven konservativen
Edge `min(Model Probability, Historical Validation Accuracy) - Best Ask`.

Fehlender/alter Quote ergibt WATCH. Kritische Forecast-Warnungen, aufgelöster Markt,
zu breiter Spread oder nichtpositiver konservativer Edge ergeben NO TRADE. Ohne HC-A/B
Trigger lautet der Status NO SIGNAL. `DATA UNAVAILABLE` wird in der App separat gezeigt.

## Aktivierung

Vor dem ersten Forward-Lauf müssen gesetzt und fachlich geprüft sein:

1. separater Cloudflare-KV-Namespace und Worker-Secret `PUBLISH_TOKEN`;
2. GitHub-Variablen `OPPORTUNITY_SCANNER_PUBLIC_BASE_URL` und
   `OPPORTUNITY_SCANNER_ACTIVATION_DATE`;
3. Secret `OPPORTUNITY_SCANNER_PUBLISH_TOKEN`;
4. Streamlit-Variable `OPPORTUNITY_SCANNER_JOURNAL_URL`;
5. Market Mapping und Bid/Ask-Semantik jedes Airports;
6. Tests inklusive Rule Hash, Testtag, DST, P&L und Production-Isolation.

Ohne diese Aktivierungswerte scheitert der Workflow geschlossen; er erzeugt weder
NO SIGNAL noch rückwirkende Shadow Evidence.

## Kontrollierter erster Lauf

1. Worker aus `opportunity-scanner-worker/` erneut deployen, damit Feed-Route und
   Journal-Route denselben dedizierten KV verwenden.
2. GitHub Action **13 - Build opportunity scanner causal feed** manuell starten.
3. `.../opportunity-scanner-feed.json` auf sechs Forecasts,
   `contains_outcomes=false` und `writes_production_database=false` prüfen.
4. Erst danach ein fachlich gewähltes `OPPORTUNITY_SCANNER_ACTIVATION_DATE` setzen.
5. GitHub Action **14 - Cross-airport Opportunity Scanner shadow** manuell starten.
6. Journal und App prüfen.

## Aktivierter Forward-Clock

Der kontrollierte Erstlauf wurde am 14. Oktober 2026 erfolgreich abgeschlossen. Workflow 13
läuft primär über den Cloudflare-Scheduler täglich von 06:07 bis 15:52 UTC im
15-Minuten-Raster (`7,22,37,52 6-15 * * *`). Der Scanner teilt sich dafür einen
Cloudflare-Cron mit dem Madrid-Collector; Madrid wird weiterhin ausschließlich zu
`:07` und `:37` gestartet. Das Fenster deckt die eingefrorenen
W60-Checkpoints der sechs Airports einschließlich Sommer-/Winterzeit ab. Ein stündlicher
GitHub-Cron (`12 6-15 * * *`) bleibt als Fallback bestehen und überspringt die teuren
Providerabrufe, wenn der veröffentlichte Feed höchstens zwölf Minuten alt und sicher
validiert ist. Workflow 14 startet nur nach einem erfolgreich abgeschlossenen Workflow 13;
fehlgeschlagene Feed-Läufe werden nicht weiterverarbeitet. Beide Workflows behalten
zusätzlich `workflow_dispatch` für kontrollierte manuelle Prüfungen.
