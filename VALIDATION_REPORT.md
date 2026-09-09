# Validation – Weatherman Madrid v1.0.9

## Ergebnis

- Basistests des hochgeladenen v1.0.7: 230 Python-Tests bestanden.
- Abschließende v1.0.9-Prüfung: **247 Python-Tests bestanden**.
- **9 Cloudflare-Worker-Tests bestanden**.
- Ruff 0.15.22: alle Prüfungen bestanden.
- `python -m compileall -q app.py src scripts`: bestanden.
- SHA-256-Vergleich: **30 geschützte Dateien bytegleich** zur gelieferten v1.0.7.
  Reproduzierbar mit `python scripts/verify_engine_baseline.py`.
- Python 3.12; lokale SQLite-Testdatenbank, isolierte Provider-/KV-Mocks.
  Streamlit-Funktionstest über AppTest; keine visuelle Browserprüfung durchgeführt.

## Wesentliche Regressionen

- Vorheriges hohes METAR sowie Folgetags-Mitternacht ausgeschlossen; Maximum, Gap,
  Vergleiche, Shadow und Grafik teilen den lokalen Madrid-Tag.
- Sommerzeitbeginn (23 Stunden), Sommerzeitende (25 Stunden) und Normalbetrieb geprüft.
- Explizite Vergleichsrundung an .5-Grenzen; kein Python-Bankers-Rounding.
- Observation-Freshness an 75/120-Minuten-Grenzen und zwischen Abrufen geprüft.
- Forecaststufen, Gewichte, Anpassungen, finale Verteilung, Locks, Challenger und
  Regime Memory stimmen für eine mehrtägige Testkohorte vor/nach SQL-Projektion überein.
- Reliability-Auswertung identisch; Features und OOS-Wahrscheinlichkeiten bleiben erhalten.
- Wiederholter Cockpit-Aufruf trifft den Cache; gezielte Invalidierung lädt erneut.
  Keine Providerdetail- oder unbenötigte Provenienz-JSON-Abfrage im Standardpfad.
- Streamlit rendert AEMET direkt nach Relevant buckets ohne Ausnahme.
- Doppelte AEMET-Antwort erhält first_seen_at und schreibt Tagesdaten nicht erneut.
- Mitternacht und späte Vortagswerte: Archiv wird zusammengeführt, alter Peak bleibt
  nicht im neuen Tag, spätes Maximum aktualisiert das Archiv.
- Providerfehler erhält letzten erfolgreichen Stand; Archivfehler getrennt sichtbar.
- Entfernter/unerwarteter Cron löst keinen Collector aus. Stündlicher AEMET-Cron,
  Collector und Closeout werden korrekt getrennt geroutet.
- Export kennzeichnet HTTP 404 als archive_missing und lehnt falsch datierte Serie ab.
- GZIP-Archive ohne HTTP-Content-Encoding werden anhand ihrer Dateisignatur erkannt
  und korrekt dekodiert; dies reproduziert den Fehler aus dem Export vom 8. September.
- Der Export zerlegt Live-Attribution künftig in additiven Live-Mittelpunkt und den
  anschließenden Effekt der Day-/Peak-Lock-Verteilungskonditionierung. Eine
  TAF-Bucket-Abweichung wird getrennt vom bestehenden TAF-Konfliktflag ausgewiesen.
- Modal-Bucket-Reliability verwendet die gespeicherte Champion-Verteilung. Ein
  Regressionstest mit Center 27,77 °C, Center-Bucket 28 und Modal-Bucket 27 bestätigt
  die getrennte Bewertung gegen Stored-METAR-Actual 27.
- Checkpoint-Darstellung enthält Modal-Bucket, Top-1-Wahrscheinlichkeit, Runner-up,
  Top-1-/Top-2-Abstand, Champion-Center und Center-Bucket.
- Live- und TAF-Attribution lassen sich einschließlich Verteilungs- und Lock-
  Konditionierung vollständig zum gespeicherten Champion-Center überleiten.

## Grenzen

Quellstand: GitHub-ZIP-Kommentar `fd8874d462c50c94cc76e328edd3886b03ed4a40`.
Der aktuelle Remote-HEAD und das laufende Streamlit-/Cloudflare-Deployment wurden
nicht live verifiziert. Es gab keinen Zugriff auf Neon oder echte Provider-Secrets.
Es wurde nichts produktiv deployt. Die Anleitung enthält diese Prüfungen vor/nach Installation.

AEMET-Rohantwort und genaue tamax-Intervall-/Peakzeitsemantik bleiben unverifiziert.
Die UI verwendet deshalb Berichtszeit und der Worker bewahrt mögliche native
Maximum-Zeitfelder ohne unbelegte Interpretation. Fehlende Altarchive sind nicht
wiederhergestellt. first_seen_lag enthält Polling-Verzögerung und ist keine exakte
Publikationslatenz. Tatsächliche Neon-Einsparung erst im Betrieb messen.

Die drei älteren Quelltexttests wurden an den verschobenen Abfragelader und den
beschlossenen Stunden-Cron angepasst. Funktionale Regressionen ergänzen diese Checks.
