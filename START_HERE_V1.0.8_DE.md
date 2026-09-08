# Weatherman Madrid v1.0.8 – Installation von v1.0.7

Die lokale Prüfung erfolgt gegen das von dir hochgeladene GitHub-ZIP mit Commit
`fd8874d462c50c94cc76e328edd3886b03ed4a40` und Paketversion 1.0.7.
Der heute aktuelle GitHub-HEAD, das tatsächlich laufende Streamlit-Deployment und
Cloudflare wurden aus dieser Arbeitsumgebung nicht live geprüft. Es wurde nichts
nach GitHub gepusht oder produktiv deployt.

## 1. Ausgangsstand vor dem Upload vergleichen

1. Öffne `weatherman84/weatherman-madrid` in GitHub und den von Streamlit verwendeten
   Branch (laut Handoff `main`).
2. Öffne den neuesten Commit. Die Kennung des gelieferten Ausgangsstands beginnt mit
   **fd8874d**. Prüfe auch `pyproject.toml`: dort muss vor dem Update `1.0.7` stehen.
3. Zeigt GitHub inzwischen einen anderen Commit, lade diesen Stand erneut herunter
   und lasse die Änderungen abgleichen. Das Paket nicht über neuere Arbeiten legen.
4. In Streamlit die App-Einstellungen prüfen: Repository `weatherman84/weatherman-madrid`,
   richtiger Branch und Einstiegspunkt `app.py`; angezeigte Engine v10.7.11.
5. Das ursprüngliche v1.0.7-ZIP als Rückfallstand behalten.

## 2. Paket entpacken und App-Dateien hochladen

Das Release-ZIP enthält:

- `UPLOAD_TO_GITHUB/`: vollständiger Quellstand von v1.0.8, einschließlich `.github`;
- `UPDATE_FROM_V1.0.7/`: nur neue/geänderte Dateien, mit den richtigen Unterordnern;
- `UPLOAD_WORKFLOWS_SEPARATELY/test.yml`: geänderte Test-Workflow-Datei;
- diese Anleitung und `VALIDATION_REPORT.md`.

**Für deine vorhandene v1.0.7 genügt `UPDATE_FROM_V1.0.7`.**

1. In GitHub im Hauptverzeichnis **Add file → Upload files** öffnen.
2. Den **Inhalt** von `UPDATE_FROM_V1.0.7` hineinziehen. Der Ordnername selbst darf
   nicht als zusätzlicher Unterordner im Repository entstehen.
3. Vor dem Commit die Pfade prüfen, insbesondere:
   - `app.py`;
   - `src/weatherman/cockpit_data.py`;
   - `src/weatherman/aemet_live.py`;
   - `src/weatherman/aemet_metar_shadow.py`;
   - `src/weatherman/daily_analysis_export.py`;
   - `src/weatherman/__init__.py`;
   - `pyproject.toml`;
   - `ENGINE_BASELINE_SHA256.json` und `scripts/verify_engine_baseline.py`;
   - `cloudflare-scheduler/src/index.js` und `cloudflare-scheduler/wrangler.jsonc`.
4. Vorhandene Dateien ersetzen. Kein bestehendes Verzeichnis löschen.
5. Commit: `Weatherman Madrid v1.0.8 – AEMET, Tagesfilter und Cockpit-Cache`.

Falls der Browser Unterordner nicht mitnimmt, im jeweiligen GitHub-Unterordner die
Dateien aus dem entsprechenden Paket-Unterordner hochladen. Für das vollständige
Paket bei einer Uploadmengen-Grenze die Verzeichnisse getrennt hochladen. Keine
`.env`, Datenbankdateien, Tokens, API-Keys oder echten Streamlit-Secrets ergänzen.

## 3. Geänderten Workflow separat ersetzen

1. In GitHub **`.github` → `workflows`** öffnen.
2. **Add file → Upload files** wählen.
3. `test.yml` aus `UPLOAD_WORKFLOWS_SEPARATELY` oder dem separaten Workflow-ZIP hochladen.
4. Commit: `v1.0.8 tests and protected engine verification`.

Nur `test.yml` wurde geändert. Collector-, Closeout- und Export-Workflows bleiben
inhaltlich unverändert. Der neue Test-Workflow prüft zusätzlich Python-Kompilierung
und die 30 geschützten Datei-Hashes. `.github` kann beim Windows-Ordnerupload fehlen,
daher dieser gesonderte Schritt.

## 4. GitHub-Tests abwarten

Unter **Actions → 0 - Tests** den neuesten Lauf abwarten. Erwartet:

- Ruff ohne Fehler;
- Python-Kompilierung ohne Fehler;
- `PASS: 30 protected files byte-identical to v1.0.7`;
- **245 Python-Tests bestanden**;
- **9 Cloudflare-Worker-Tests bestanden**.

Bei einem Fehler das Deployment nicht als abgenommen betrachten. Den fehlgeschlagenen
Schritt öffnen und die Fehlermeldung ohne Secrets zur Prüfung bereitstellen.

## 5. Bestehenden Cloudflare Worker aktualisieren

AEMET-Key, GitHub-Token und KV-Binding bestehen bereits. Sie müssen nicht neu angelegt
werden. Den vorhandenen Worker und den vorhandenen Namespace weiterverwenden.

1. In Cloudflare **Workers & Pages → dein Madrid-Scheduler** öffnen.
2. Prüfen, dass das KV-Binding **`AEMET_HOT`** auf den bisherigen Namespace zeigt.
   Ein leerer neuer Namespace würde bisherige Archive nicht enthalten.
3. **Edit code** öffnen.
4. Den Code vollständig durch `cloudflare-scheduler/src/index.js` aus v1.0.8 ersetzen.
5. **Deploy** wählen.
6. Direkt danach unter **Settings → Triggers → Cron Triggers** den bisherigen
   AEMET-Trigger `*/10 * * * *` entfernen und `50 * * * *` hinzufügen.
7. Prüfen, dass exakt diese drei Trigger bestehen:

| Aufgabe | Cron in UTC |
|---|---|
| AEMET stündlich | `50 * * * *` |
| Madrid Collector, Aviation/Fixpunkte | `7,37 5-20 * * *` |
| Madrid Closeout | `15 19,20 * * *` |

**Code zuerst, Cron unmittelbar danach.** Der neue Worker ignoriert unbekannte
Cron-Ausdrücke, einschließlich des alten Zehn-Minuten-Triggers. Während der kurzen
Umstellung kann dadurch ein AEMET-Lauf ausfallen; er wird nicht zum Collector-Lauf.
Nur den Cron unter dem alten Worker zu ändern wäre unsicher.

Ein GitHub-Upload allein deployt den Cloudflare Worker nicht. `wrangler.jsonc` und
die Worker-Konstante enthalten denselben neuen Cron. Wer Wrangler statt des
Dashboard-Editors verwendet, muss seine vorhandene lokale KV-Binding-Konfiguration
beibehalten; die Repository-Datei enthält absichtlich keine echte Namespace-ID.

## 6. Cloudflare prüfen

Die öffentliche Worker-Startadresse öffnen. Erwartet werden:

- `aemet_cron_utc: "50 * * * *"`;
- `aemet_station: "3129"`;
- `aemet_hot_store_configured: true`;
- `aemet_key_configured: true`.

Nach Aktivierung des Triggers den nächsten stündlichen `:50`-Lauf abwarten und prüfen:

- `/aemet-live.json`: `schema_version: "1.1"`, letzter Abruf, Providerstatus,
  Beobachtungsalter und `archive_status`;
- `/aemet-today.json`: korrektes lokales Madrid-Datum, Stundenpunkte,
  `first_seen_at` und `first_seen_lag_minutes` je neu erkanntem Datensatz;
- `/archive/aemet/YYYY/MM/DD.json.gz`: vorhandene frühere Tage.

`:50` ist ein vorläufiger Abruftermin, keine garantierte AEMET-Publikationsminute.
Ein erfolgreicher Abruf darf ältere Beobachtungen enthalten. `delayed` kann deshalb
auch bei `provider_status: success` auftreten. Die Grenzen sind ≤75 Minuten
`current`, >75 bis einschließlich 120 `delayed`, darüber `stale`.

Um Mitternacht kann die neue Tageskurve noch leer sein. Das Maximum des Vortags wird
nicht als heutiges Maximum angezeigt. Verfügbare späte Vortagswerte werden dem
vorhandenen Archiv hinzugefügt. HTTP 404 wird als fehlendes Archiv ausgewiesen.
Die sechs zuvor fehlenden Archive werden nicht erfunden oder interpoliert; nur noch
vom Provider gelieferte Werte können wiedergewonnen werden.

## 7. Streamlit prüfen

1. Auf das Deployment des neuen GitHub-Commits warten und die App neu öffnen.
2. Die Kopfzeile muss **App v1.0.8 · Engine v10.7.11 · protected forecast baseline
   v10.7.10** anzeigen.
3. Zeigt die App nach abgeschlossenem Deployment noch den alten Stand, unter
   **Manage app → Reboot app** einmal neu starten.
4. Die bestehenden `DATABASE_URL` und `AEMET_PUBLIC_BASE_URL` bleiben unverändert.
   Keine neue Neon-Datenbank und keine neue manuelle Tabellenmigration erforderlich.
5. Den gewählten Tag prüfen: AEMET steht direkt nach **3 · Relevant buckets**.
6. Drei Reihen mit je drei Hauptmetriken, vollständige Zeitangaben darunter und
   Hilfetexte bei den Metriken prüfen.
7. `Stored METAR max` und Kurvenpunkte dürfen ausschließlich den gewählten lokalen
   Madrid-Tag enthalten, auch an Tagen mit Zeitumstellung.
8. In der Vergleichstabelle stehen AEMET-Vergleichsbucket, gemeldeter METAR-Bucket
   und `MATCH`/`DIFF`. Die Vergleichsrundung ist ausdrücklich definiert und ist
   keine bestätigte Polymarket-Rundungsregel.
9. Die Dezimalkurve, METAR-Punkte und Tmax-Markierung bleiben vorhanden.
10. **Refresh Madrid now** einmal ausführen. Die betroffenen Cockpit-Caches werden
    nach dem erfolgreichen Datenabruf und nochmals nach Checkpoints geleert.
    Ein anschließender Seiten-Rerun liest den aktualisierten Stand.

Das AEMET-Fragment aktualisiert sich weiterhin alle fünf Minuten ausschließlich aus
Cloudflare; es berechnet den Champion nicht neu und liest nicht aus Neon. Seine
METAR-Punkte sind der Stand des letzten vollständigen Cockpit-Ladens. Neue METARs
erscheinen beim nächsten vollständigen Rerun bzw. manuellen Refresh.

## 8. Daily-Analysis-Export aktualisieren

1. GitHub **Actions → 6 - Publish Madrid daily-analysis export → Run workflow**.
2. Erfolgreichen Build und die Prüfung der veröffentlichten Zeit abwarten.
3. Im Export prüfen:
   - App-Version 1.0.8 (Feld `application_version`);
   - `export_engine_version: "v10.7.11"`;
   - `protected_forecast_baseline: "v10.7.10"`;
   - AEMET `public_series_cadence_minutes: 60` und `feed_health`;
   - fehlende Archive: `availability_reason: "archive_missing"`;
   - `market_resolution_actual: null` im AEMET-Bereich.

Das bisherige Feld `forecast_engine_baseline` bleibt aus Kompatibilitätsgründen
v10.7.11. Das neue Feld `protected_forecast_baseline` benennt die fachliche Baseline
korrekt. Export-Schema 1.3 bleibt bestehen; die Zusatzfelder sind additiv.

## 9. Neon-Transfer beobachten

Unter **Historical detail data and transfer diagnostics** stehen Zeilenanzahl,
Spaltenanzahl, Ladezeit und geschätzte Arbeitsspeichergröße pro Abfragegruppe.
Das Öffnen dieser Übersicht löst keine weiteren Datenbankabfragen aus. Vollständige
Snapshot-/Varianten-JSONs werden erst mit **Load full historical snapshots and
variants** geladen; Providerdetails erst mit **Load provider call details**.

Die Größen sind **keine gemessenen Neon-Netzwerkbytes**. Vergleiche die tatsächliche
Neon-Verbrauchsentwicklung an den nächsten Tagen bei ähnlicher App-Nutzung. Es wird
keine bestimmte prozentuale Einsparung versprochen. Daten sind höchstens fünf Minuten
im Cache; Collector- und Closeout-Läufe invalidieren diesen Cache nicht sofort.

Die bestehenden Fenster für Kalibrierung und OOS bleiben erhalten: 90 Tage historische
D1-Forecasts, 400 Tage Actuals, 120 Tage Snapshots und 90 Tage Varianten. Features und
OOS-Wahrscheinlichkeiten werden weiterhin geladen. Die Collector-Kadenz bleibt gleich.

## 10. Fachliche Grenzen und Rückfall

- `tamax` wird als vom Provider gelieferter Maximalwert gespeichert. Die genaue
  Dauer und Abgrenzung seines Messintervalls sind für den produktiven Endpunkt noch
  nicht verifiziert. Stundenabstand von `fint` beweist keine 60-Minuten-Extremperiode.
- Ohne verifiziertes Peakfeld heißt die Zeit **Tmax report time**. `fint` bleibt aus
  Kompatibilitätsgründen zusätzlich als `observed_at` erhalten, mit `report_at` und
  `peak_time_verified: false`. Mögliche native Maximum-Zeitfelder werden als
  `maximum_time_fields` gesichert und nicht mit erfundener Zeitzone umgedeutet.
- `first_seen_at` ist der erste Abruf, bei dem dieser Worker den Wert erfasst hat.
  Die Differenz enthält das stündliche Polling und ist keine exakte Providerlatenz.
  Zu Beginn mitgelieferte historische Werte sind für eine Publikationsanalyse separat
  zu behandeln. Alte Erst-Erkennungszeiten werden nicht rückwirkend erfunden.
- Die Behauptung „physischer Peak exakt um 18:00 LT“ bleibt damit offen. Für eine
  spätere Bestätigung genügt eine bereinigte Rohantwort samt Provider-Metadaten;
  keine API-Keys im Chat oder Export.
- Physical Stall und METAR Persistence bleiben unkalibriert, ohne Wahrscheinlichkeit
  und mit `champion_impact_c = 0.0`. Alle Research-Watchlists bleiben erhalten.

Rückfall: den letzten funktionierenden GitHub-Stand wiederherstellen. Wird auch der
alte Worker zurückgespielt, zuerst dessen passenden AEMET-Cron wieder auf
`*/10 * * * *` setzen, solange der neue Worker unbekannte Trigger noch sicher
ignoriert; dann den alten Code deployen. Alte Worker-Version und neuer `:50`-Cron
sollten nicht zusammen laufen. Den KV-Namespace nicht löschen. Neue Peak-Metadaten
und `first_seen_at` können vom alten Writer verloren gehen; v1.0.8 daher bevorzugt
gezielt reparieren.
