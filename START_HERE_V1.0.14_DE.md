# Weatherman Madrid v1.0.14 – AEMET-Binding-Hotfix

v1.0.14 baut direkt auf v1.0.13 auf. Forecast Engine v10.7.11 und die geschützte
Forecast-Baseline v10.7.10 bleiben unverändert.

## Ursache

`AEMET_HOT` war bisher nur im Cloudflare-Dashboard gebunden, aber nicht in
`cloudflare-scheduler/wrangler.jsonc` deklariert. Ein Deployment mit Wrangler konnte
die Dashboard-Konfiguration deshalb ohne dieses Binding veröffentlichen. Die
öffentlichen AEMET-Endpunkte antworten dann absichtlich mit HTTP 503.

## Dauerhafter Fix

Die vorhandene Namespace-ID ist nun in der Worker-Konfiguration verankert:

- Binding: `AEMET_HOT`
- Namespace: `weatherman-madrid-aemet-hot`
- Namespace-ID: `a6d21e1736244b4d8dffa296f6664aff`

Die Namespace-ID ist kein Zugangsschlüssel. Folgende Werte bleiben Secrets und dürfen
nicht in Dateien oder Chat eingefügt werden:

- `AEMET_API_KEY`
- `GITHUB_TOKEN`
- `DAILY_ANALYSIS_PUBLISH_TOKEN`

## Cloudflare einmalig aktualisieren

1. **Workers & Pages → weatherman-madrid-scheduler → Settings → Bindings** öffnen.
2. Falls `AEMET_HOT` aktuell fehlt, ein KV-Namespace-Binding mit exakt diesem Namen
   und dem Namespace `weatherman-madrid-aemet-hot` anlegen.
3. Den Worker aus dem Verzeichnis `cloudflare-scheduler` mit der versionierten
   `wrangler.jsonc` deployen. Dadurch bleibt das Binding bei folgenden Wrangler-
   Deployments erhalten.
4. Die Worker-Basisadresse öffnen. Erwartet:
   - `aemet_hot_store_configured: true`
   - `aemet_key_configured: true`
   - `aemet_cron_utc: "50 * * * *"`
5. Nach dem nächsten Lauf zur UTC-Minute `:50` prüfen:
   - `/aemet-live.json`
   - `/aemet-today.json`

Ein leerer Store kann bis zum ersten erfolgreichen `:50`-Lauf HTTP 404 liefern. HTTP
503 mit `AEMET_HOT KV binding is not configured` bedeutet weiterhin, dass nicht die
versionierte Worker-Konfiguration deployt wurde.

## Schutzgrenzen

- keine Forecast- oder Champion-Änderung;
- keine Änderung an Biases, TAF, Regimes oder Locks;
- keine Neon-Abfrage und kein Backfill;
- AEMET bleibt eine getrennte physische Stationsserie und kein Market-Actual;
- `research_only=true`, `automatic_promotion=false`.
