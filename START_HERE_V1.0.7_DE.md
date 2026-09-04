# Weatherman Madrid v1.0.7 installieren

Du verwendest noch v1.0.4. Deshalb wird **kein Zwischenupdate** benötigt:
v1.0.7 enthält alle Änderungen aus v1.0.5 und v1.0.6 sowie die zusätzliche
AEMET-/METAR-Härtung und die getrennten Shadow-Diagnostiken.

## 1. Vollständiges GitHub-Paket hochladen

1. `weatherman-madrid-v1.0.7.zip` entpacken.
2. `UPLOAD_TO_GITHUB` öffnen.
3. Den gesamten **Inhalt** dieses Ordners in die Wurzel von
   `weatherman84/weatherman-madrid` hochladen und vorhandene Dateien ersetzen.
4. Commit-Nachricht:
   `Weatherman Madrid v1.0.7 AEMET live observations`.

Keine `.env`, Datenbankdatei oder Zugangsdaten hochladen.

## 2. Versteckte Workflow-Dateien separat hochladen

Der Browser nimmt `.github` bei einem Ordnerupload häufig nicht mit. Deshalb danach
im Repository ausdrücklich `.github → workflows` öffnen und über
**Add file → Upload files** diese vier Dateien aus `UPLOAD_WORKFLOWS_SEPARATELY`
hochladen beziehungsweise ersetzen:

- `madrid-collector.yml`;
- `madrid-closeout.yml`;
- `publish-daily-analysis-export.yml`;
- `test.yml`.

Commit-Nachricht: `Update v1.0.7 workflows`.

## 3. Tests abwarten

Unter **Actions → 0 - Tests** muss der jüngste Lauf grün sein:

- `230 passed` für Python;
- drei erfolgreiche Cloudflare-Worker-Tests;
- Ruff erfolgreich.

Die Forecast-Engine bleibt v10.7.11. Forecastformel, Madrid-Anchor, Gewichte, Biases,
Regimes, Locks und Promotion-Gates wurden nicht verändert.

## 4. AEMET und Cloudflare einrichten

Arbeite anschließend die Datei `CLOUDFLARE_SETUP_DE.md` vollständig ab. Neu sind:

1. AEMET-OpenData-Key anfordern;
2. kostenlosen Workers-KV-Namespace `weatherman-madrid-aemet-hot` erstellen;
3. ihn als `AEMET_HOT` an den bestehenden Worker binden;
4. `AEMET_API_KEY` als Cloudflare-Secret speichern;
5. neuen Worker-Code deployen;
6. AEMET-Cron `*/10 * * * *` ergänzen;
7. Collector auf `7,37 5-20 * * *` stellen;
8. Closeout `15 19,20 * * *` beibehalten.

Der AEMET-Key kommt weder in GitHub noch in Streamlit.

## 5. Öffentliche Worker-Origin eintragen

Dieselbe öffentliche Worker-Origin ohne Dateipfad wird an zwei Stellen eingetragen:

### Streamlit Secrets

```toml
AEMET_PUBLIC_BASE_URL = "https://weatherman-madrid-scheduler.DEIN-SUBDOMAIN.workers.dev"
```

### GitHub Actions Variable

Repository → **Settings → Secrets and variables → Actions → Variables**:

- Name: `AEMET_PUBLIC_BASE_URL`;
- Wert: dieselbe Worker-Origin.

Die URL ist öffentlich und daher kein Secret. `AEMET_API_KEY` bleibt nur in Cloudflare.

## 6. Streamlit-Deployment

Streamlit übernimmt den GitHub-Commit automatisch. Kein Computer- oder Server-Reboot
ist nötig. Nach einigen Minuten die App neu öffnen.

Nur wenn nach etwa fünf Minuten weiterhin eindeutig v1.0.4 angezeigt wird:
**Manage app → Reboot app** einmal ausführen.

## 7. Abnahme

1. In der Kopfzeile steht Engine v10.7.11; Paketversion ist v1.0.7.
2. Der AEMET-Bereich zeigt Station 3129, Dezimaltemperatur, Physical Tmax,
   gespeichertes METAR-Maximum, Reihenlücke, Tmax-Zeit, Datenalter und Status.
3. Die Kurve enthält AEMET als Linie und LEMD METAR als getrennte Punkte.
4. Fünf Minuten warten: Nur der AEMET-Bereich aktualisiert sich; der Champion wird
   nicht neu berechnet.
5. Manueller `Refresh Madrid now` speichert weiterhin einen zusätzlichen
   `manual-live`-OOS-Snapshot, löst aber keinen AEMET-API-Aufruf aus.
6. Workflow 6 einmal manuell starten. Im veröffentlichten Export müssen stehen:
   - `schema_version: "1.3"`;
   - `aemet_physical_observations.configured: true`;
   - `market_resolution_actual: null`, solange die Polymarket-Regel unbestätigt ist.

## 8. Fachliche Trennung

- `stored_metar_max`: höchster ganzzahlig gespeicherter Flughafenwert;
- `aemet_physical_tmax`: offizielles physisches Maximum mit Dezimalstelle;
- `market_resolution_actual`: bleibt leer, bis Quelle und Rundungsregel des Marktes
  eindeutig bestätigt sind.

AEMET verändert in v1.0.7 weder Champion noch Forecast-Center. Physical Stall und
METAR Bucket Persistence erscheinen ausschließlich als unkalibrierte `SHADOW`-Signale.
Sie zeigen keine Prozentwahrscheinlichkeit und benötigen eigene sequenzielle OOS-Evidenz.
