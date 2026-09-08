# Cloudflare Scheduler und AEMET-Livecache – Madrid v1.0.8

Für das Update von v1.0.7 zuerst `START_HERE_V1.0.8_DE.md` verwenden. Bestehende
Secrets und KV-Bindings beibehalten. Worker-Code zuerst deployen, danach den alten
AEMET-Cron ersetzen. Unbekannte Trigger werden vom neuen Worker ignoriert.

## Zielarchitektur

Der bestehende Cloudflare Worker erfüllt ab v1.0.8 zwei voneinander unabhängige Aufgaben:

1. Er dispatcht den leichten Madrid-Collector alle 30 Minuten, die vier vollständigen
   Fixpunkte und den 21:15-LT-Tagesabschluss nach GitHub Actions.
2. Er ruft AEMET Station `3129` einmal pro Stunde um Minute :50 direkt ab und speichert ausschließlich
   zwei kleine öffentliche Zustände in Workers KV:
   - `aemet-live.json`: letzter Status, wird überschrieben;
   - `aemet-today.json`: heutige deduplizierte Dezimalkurve, wird nur bei einer neuen
     Messung aktualisiert.

Beim Datumswechsel wird der abgeschlossene Tag unter
`archive/aemet/YYYY/MM/DD.json.gz` komprimiert archiviert. AEMET schreibt nicht nach
Neon und startet keine GitHub Action. Ein AEMET-Fehler berührt weder Collector noch
Champion.

## 1. Zuerst v1.0.8 nach GitHub hochladen

1. Das vollständige v1.0.8-Paket entpacken.
2. Den Inhalt von `UPLOAD_TO_GITHUB` in die Wurzel von
   `weatherman84/weatherman-madrid` hochladen und vorhandene Dateien ersetzen.
3. Weil `.github` beim Browser-Upload häufig ausgelassen wird, danach ausdrücklich
   `.github/workflows` öffnen und die geänderte Datei aus
   `UPLOAD_WORKFLOWS_SEPARATELY` einzeln hochladen:
   - `test.yml`.
4. Commit-Nachricht: `Weatherman Madrid v1.0.8 AEMET live observations`.
5. **Actions → 0 - Tests** abwarten. Erwartet werden 245 Python-Tests und neun
   Cloudflare-Worker-Tests.

## 2. AEMET-API-Key anfordern

1. [AEMET OpenData](https://opendata.aemet.es/centrodedescargas/inicio) öffnen.
2. Unter **Obtención de API Key** auf **Solicitar** klicken.
3. E-Mail-Adresse eingeben und die zugesandten Bestätigungsschritte ausführen.
4. Den endgültigen API-Key kopieren.

Der Key wird ausschließlich als Cloudflare-Secret gespeichert. Er kommt nicht in
GitHub, Streamlit, Repository-Dateien oder ChatGPT.

## 3. Workers-KV-Namespace anlegen

1. Cloudflare-Dashboard öffnen.
2. **Storage & Databases → KV** beziehungsweise **Workers KV** öffnen.
3. **Create namespace** wählen.
4. Name: `weatherman-madrid-aemet-hot`.
5. Namespace erstellen.

Dieser Namespace enthält nur die zwei kleinen Livewerte und komprimierte Tagesarchive.
Es wird keine Datenbankdatei gespeichert.

## 4. KV an den bestehenden Worker binden

1. **Workers & Pages → weatherman-madrid-scheduler** öffnen.
2. **Settings → Bindings** öffnen.
3. **Add binding → KV namespace** wählen.
4. Variable name exakt: `AEMET_HOT`.
5. KV namespace: `weatherman-madrid-aemet-hot`.
6. Speichern beziehungsweise deployen.

Der Variablenname ist technisch verbindlich. Ein anderer Name wird vom Worker nicht
erkannt.

## 5. AEMET-Key als Worker-Secret speichern

1. Im selben Worker **Settings → Variables and Secrets** öffnen.
2. **Add** wählen.
3. Name exakt: `AEMET_API_KEY`.
4. Typ ausdrücklich **Secret**.
5. Als Wert den AEMET-OpenData-Key einsetzen.
6. Speichern und deployen.

Die vorhandenen Werte bleiben unverändert:

| Name | Typ |
|---|---|
| `GITHUB_OWNER=weatherman84` | Text |
| `GITHUB_REPO=weatherman-madrid` | Text |
| `GITHUB_REF=main` | Text |
| `GITHUB_TOKEN` | Secret |
| `AEMET_API_KEY` | Secret |

## 6. Worker-Code aktualisieren

1. Im Worker **Edit code** öffnen.
2. Den bisherigen Code vollständig durch
   `cloudflare-scheduler/src/index.js` aus dem v1.0.8-Paket ersetzen.
3. **Deploy** wählen.

Der Worker gibt weder AEMET- noch GitHub-Key aus. Öffentlich sind nur die bereinigten
Stationsbeobachtungen.

## 7. Drei Cron Trigger einstellen

Unter **Settings → Triggers → Cron Triggers** müssen exakt diese drei Trigger stehen:

| Zweck | Cron (UTC) |
|---|---|
| AEMET Station 3129 | `50 * * * *` |
| Madrid Aviation/Fixpunkte | `7,37 5-20 * * *` |
| DST-sicherer Closeout | `15 19,20 * * *` |

Den vorübergehend eingestellten stündlichen Collector-Trigger `7 5-20 * * *` löschen.
Einen alten 15-Minuten-Trigger `7,22,37,52 5-20 * * *` ebenfalls löschen.

Der AEMET-Cron läuft unabhängig von GitHub. Der Closeout-Cron wird zweimal in UTC
ausgelöst; der Worker verwendet nur den Lauf, der tatsächlich 21:15 Madrid-Zeit ist.

## 8. Öffentliche Worker-Adresse prüfen

1. Auf der Worker-Übersicht die `workers.dev`-Adresse öffnen, zum Beispiel:
   `https://weatherman-madrid-scheduler.DEIN-SUBDOMAIN.workers.dev`
2. Die Startantwort muss enthalten:
   - `aemet_station: "3129"`;
   - `aemet_hot_store_configured: true`;
   - `aemet_key_configured: true`;
   - `aemet_cron_utc: "50 * * * *"`.
3. Nach dem nächsten stündlichen :50-Lauf öffnen:
   - `/aemet-live.json`;
   - `/aemet-today.json`.
4. Beide Antworten müssen Station `3129` und die Klassifikation
   `AEMET PHYSICAL OBSERVATIONS — NOT MARKET RESOLUTION` enthalten.

Wenn `not found` erscheint, zuerst den nächsten stündlichen :50-Lauf abwarten und dann unter **Logs** nach
`aemet-stored` beziehungsweise einem klaren `AEMET refresh failed` suchen.

## 9. Öffentliche Worker-Origin in Streamlit eintragen

1. Streamlit Community Cloud öffnen.
2. Madrid-App → **Settings → Secrets**.
3. Diese Zeile ergänzen; nur die Origin einsetzen, keinen Dateipfad:

```toml
AEMET_PUBLIC_BASE_URL = "https://weatherman-madrid-scheduler.DEIN-SUBDOMAIN.workers.dev"
```

4. Speichern.

Kein AEMET-Key kommt in Streamlit. Die App liest nur die bereinigten öffentlichen
Dateien. Streamlit übernimmt den neuen GitHub-Commit automatisch; ein Reboot ist im
Normalfall nicht erforderlich.

## 10. Worker-Origin als GitHub-Variable eintragen

Damit Workflow 6 die physischen AEMET-Tage in den Research-Export einbezieht:

1. GitHub-Repository → **Settings → Secrets and variables → Actions**.
2. Den Reiter **Variables** öffnen.
3. **New repository variable** wählen.
4. Name: `AEMET_PUBLIC_BASE_URL`.
5. Wert: dieselbe `https://...workers.dev`-Origin ohne Dateipfad.

Das ist bewusst eine normale Variable und kein Secret: Die URL ist öffentlich. Der
AEMET-Key bleibt ausschließlich in Cloudflare.

## 11. Funktionsprüfung

1. Streamlit-App neu öffnen.
2. Der Bereich **AEMET 3129 · Physical station observations** muss erscheinen.
3. Prüfen:
   - aktuelle Dezimaltemperatur;
   - Physical Tmax und Zeitpunkt;
   - Messzeit, Datenalter und Status;
   - rote AEMET-Linie und blaue ganzzahlige METAR-Punkte.
4. Die Seite fünf Minuten offen lassen. Nur der AEMET-Bereich wird aktualisiert; der
   Zeitstempel `Calculated` der Current Decision darf sich dadurch nicht verändern.
5. **Actions → 6 - Publish Madrid daily-analysis export** einmal manuell starten.
6. Im Export muss `schema_version: "1.3"` und
   `aemet_physical_observations.configured: true` stehen.

## Verbrauch und Sicherheit

- Regulär 24 AEMET-Abrufe pro Tag, jeweils ein Live-Write und bei neuen oder
  korrigierten Beobachtungen ein Tageskurven-Write. Hinzu kommen geänderte Archive.
- AEMET-Archive benötigen voraussichtlich nur wenige Megabyte pro Jahr.
- AEMET erzeugt keine Neon-Abfragen, keine Modellabrufe und keine zusätzlichen
  GitHub-Workflow-Runs.
- AEMET Physical Tmax, LEMD METAR und ein später bestätigter Polymarket-Resolution-Wert
  bleiben drei klar getrennte Rollen.

## Zeit- und Archivsemantik in v1.0.8

Stündliche öffentliche Serie. `fint` ist die Berichtszeit, kein bestätigter exakter
Peakzeitpunkt. Dauer und Abgrenzung von `tamax` bleiben bis zur Rohdaten-/Metadatenprüfung
unverifiziert. Mögliche native Peakzeitfelder werden unverändert gesichert.
`first_seen_at` misst die erste Erkennung durch diesen Worker und enthält Polling-Latenz.
Die Observation-Freshness ist ≤75 Minuten current, >75–120 delayed, >120 stale;
Providerstatus und letzter erfolgreicher Abruf werden getrennt geführt.
Archive werden bei verspäteten Werten ergänzt. Fehlende historische Tage bleiben
fehlend, wenn der Provider keine Werte mehr liefert.
