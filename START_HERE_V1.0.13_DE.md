# Weatherman Madrid v1.0.13 installieren

v1.0.13 ist ein kumulatives Update direkt von der derzeit installierten v1.0.11.
v1.0.12 muss vorher nicht installiert werden. Forecast Engine v10.7.11 und geschützte
Baseline v10.7.10 bleiben unverändert.

## 1. Zwei identische Secrets vorbereiten

Erzeuge lokal einen langen zufälligen Wert mit mindestens 32 Zeichen. Diesen Wert
nicht in Dateien oder Chat kopieren.

### Cloudflare

1. **Workers & Pages → weatherman-madrid-scheduler → Settings → Variables and Secrets**.
2. Neues Secret: `DAILY_ANALYSIS_PUBLISH_TOKEN`.
3. Zufallswert einsetzen und speichern.

### GitHub

1. Repository → **Settings → Secrets and variables → Actions → Secrets**.
2. **New repository secret**.
3. Name: `DAILY_ANALYSIS_PUBLISH_TOKEN`.
4. Exakt denselben Zufallswert einsetzen.

Das vorhandene Repository-Variable `AEMET_PUBLIC_BASE_URL` bleibt die öffentliche
Worker-Origin. Kein neues KV-Binding anlegen. `AEMET_HOT` muss bestehen bleiben.

## 2. Update nach GitHub hochladen

1. Inhalt des v1.0.13-Updatepakets in Branch `main` hochladen und Dateien ersetzen.
2. Die geänderte versteckte Datei
   `.github/workflows/publish-daily-analysis-export.yml` zusätzlich aus dem separaten
   Workflow-Paket hochladen.
3. Unter **Actions → 0 - Tests** den erfolgreichen Lauf abwarten.

## 3. Cloudflare-Worker aktualisieren

1. Worker **weatherman-madrid-scheduler → Edit code** öffnen.
2. Code durch `cloudflare-scheduler/src/index.js` aus v1.0.13 ersetzen.
3. Deployen.
4. Bestehende Cron-Trigger und das Binding `AEMET_HOT` nicht ändern.
5. Worker-Basisadresse öffnen. Erwartet:
   `daily_analysis_mirror_configured: true`.

## 4. Replay-Schema aus v1.0.12 übernehmen

Unter **Actions → 4 - Prepare isolated replay lab → Run workflow** einmal starten.
Dadurch werden in der isolierten Replay-Datenbank nur `checkpoint_status` und
`checkpoint_reconstructed` ergänzt. Production bleibt read-only. Alte v1.0.11-
Entscheidungen bleiben unverändert; neue Provenienzfelder sind dort `null`.

## 5. Export und beide Endpunkte testen

1. **Actions → 6 - Publish Madrid daily-analysis export → Run workflow** starten.
2. Im Lauf müssen Build und Deploy grün sein.
3. Artefakt `madrid-daily-analysis-publication` herunterladen.
4. In `daily-analysis-publication-report.json` müssen beide Statuswerte
   `verified` sein:
   - `github_pages`
   - `cloudflare_kv_mirror`
5. Prüfen:
   - GitHub Pages: `/daily-analysis-latest.json`
   - Worker: `/daily-analysis-latest.json`
   - Worker datiert: `/daily-analysis/YYYY-MM-DD.json`
   - Worker Metadaten: `/daily-analysis-publication.json`

Der Bericht nennt `target_date`, `generated_at`, `size_bytes` und SHA-256. Workflow 6
berechnet den Export nur einmal. Das Spiegeln verursacht null zusätzliche Neon-Abfragen.

## 6. ChatGPT-Automation erst danach umstellen

Der Worker-Link muss einmal aus der ChatGPT-Automationsumgebung geöffnet und inhaltlich
validiert werden. Erst danach den Vertrag aus
`CHAT_AUTOMATION_FALLBACK_V1.0.13.md` in die Automation übernehmen und sie wieder
aktivieren. Ein normaler Browsertest allein erfüllt diese Freigabe nicht.

## 7. Research-Export

Workflow 9 zuerst mit `dry_run=true`, danach bei Bedarf mit `dry_run=false` und
`days=30` starten. v1.0.13 enthält die drei getrennten Evidenzsichten aus v1.0.12 und
den wirkungslosen `late_live_overlap_guard_candidate_v0.1`.

## Sicherheitsgrenzen

- keine zweite Forecast-/Exportberechnung;
- keine zusätzliche Neon-Abfrage für den Mirror;
- exakt dieselben JSON-Bytes an beiden Endpunkten;
- Worker akzeptiert höchstens 5 MiB;
- datierte KV-Dateien laufen nach 90 Tagen ab;
- kein neuer Cloudflare-Cron und kein neues Binding;
- `research_only=true`, `automatic_promotion=false`;
- keine Änderung an Forecastformel, TAF, Clear-Sky Override oder Locks.
