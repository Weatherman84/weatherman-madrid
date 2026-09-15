# Validation – Weatherman Madrid v1.0.11

## Ergebnis

- Basistests des hochgeladenen v1.0.7: 230 Python-Tests bestanden.
- Abschließende v1.0.11-Prüfung: **254 Python-Tests bestanden**.
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
- Market-Replay-Export verwendet je Checkpoint ausschließlich den letzten gespeicherten
  Snapshot mit `captured_at <= checkpoint_at`. Ein synthetischer, nur eine Minute nach
  dem Checkpoint liegender Snapshot wird vom Regressionstest sicher ausgeschlossen.
- Exact- und nearest-before-Provenienz, Snapshot-Alter, explizite Unavailable-Zeilen,
  vier Madrid-Fix-Checkpoints und die reduzierten Bucketfelder sind geprüft.
- Der manuelle Workflow nutzt nur `DATABASE_URL`, `contents: read`, ein kurzlebiges
  Download-Artefakt und die explizite read-only Transaktion mit Rollback.
- Trading-Challenger-Allokationen, externer 30-%-TAF-Hedge, Late-Live-Regel,
  Observe-only-Checkpoints und alle vier Market-Freshness-Bänder sind geprüft.
- Regime-Matrix erzeugt getrennte globale/Checkpoint-Kohorten, kennzeichnet kleines N
  und berechnet Modal-/Top-2-/Top-3- sowie Center- und TAF-Metriken parallel.
- Dry-Runs führen null Production-Abfragen aus; feste Tages-/Row-Limits und das
  getrennte Replay-Schema sind per Regressionstest abgesichert.

## Grenzen

Geprüfter Repository-Ausgangsstand: Remote-HEAD
`f9035b98e4e41ee8491f484513cf96ed2d868f40`, Paketversion v1.0.10.
Das laufende Streamlit-/Cloudflare-Deployment wurde nicht live verifiziert. Es gab
keinen Zugriff auf Neon oder echte Provider-Secrets.
Es wurde nichts produktiv deployt. Die Anleitung enthält diese Prüfungen vor/nach Installation.

AEMET-Rohantwort und genaue tamax-Intervall-/Peakzeitsemantik bleiben unverifiziert.
Die UI verwendet deshalb Berichtszeit und der Worker bewahrt mögliche native
Maximum-Zeitfelder ohne unbelegte Interpretation. Fehlende Altarchive sind nicht
wiederhergestellt. first_seen_lag enthält Polling-Verzögerung und ist keine exakte
Publikationslatenz. Tatsächliche Neon-Einsparung erst im Betrieb messen.

Die drei älteren Quelltexttests wurden an den verschobenen Abfragelader und den
beschlossenen Stunden-Cron angepasst. Funktionale Regressionen ergänzen diese Checks.

Der tatsächliche Inhalt und die Coverage des Market-Exports können erst nach dem
manuellen GitHub-Workflow gegen Production beurteilt werden. Insbesondere bleiben
historische Trade-Price-Samples nicht-ausführbare Preisbeobachtungen ohne rekonstruierte
Bid-/Ask-Historie; diese Einschränkung wird im Export ausdrücklich mitgeführt.
