# Weatherman Madrid v1.0.11 installieren

Diese Version ergänzt ausschließlich additive Research-Funktionen. Die produktive
Forecast Engine bleibt v10.7.11, die geschützte fachliche Baseline v10.7.10.

## Neue Bestandteile

- Workflow **8 - Export Madrid market replay** (Nummerndopplung behoben);
- Workflow **9 - Export Madrid research tracks** für `regime-replay-export.json`;
- Workflow **10 - Capture Madrid trading shadow** für das vorwärts laufende
  `trading_challenger_v0.1`-Journal;
- separate App-Sektion **Research · Shadow Mode**;
- konsolidierte Research-Watchlist bis einschließlich 13. September in
  `RESEARCH_WATCHLIST_V1.0.11.md`;
- zwei ausschließlich in der isolierten Replay-Datenbank angelegte Tabellen:
  `replay_lab.trading_shadow_decisions` und
  `replay_lab.trading_shadow_outcomes`.

## Installation

1. Alle Dateien dieses Pakets in den bestehenden Repository-Branch `main` hochladen.
   Beim GitHub-Upload **auch den versteckten Ordner `.github/workflows`** übernehmen.
2. Unter **Actions → 0 - Tests** den Lauf abwarten.
3. Unter **Actions → 4 - Prepare isolated replay lab → Run workflow** einmal starten.
   Dieser Schritt legt nur die beiden neuen Research-Tabellen in der durch
   `REPLAY_DATABASE_URL` bezeichneten isolierten Datenbank an. Er schreibt niemals
   in Production. Fehlt das Secret, zuerst unter **Settings → Secrets and variables
   → Actions** ergänzen.
4. Unter **Actions → 9 - Export Madrid research tracks → Run workflow** zuerst mit
   `dry_run=true` starten. Der Lauf fragt Neon nicht ab und zeigt nur Grenzen und
   Schätzung. Danach bei Bedarf mit `dry_run=false` und `days=30` ausführen und das
   Artifact `madrid-research-tracks-v0.1` herunterladen.
5. Streamlit übernimmt v1.0.11 nach dem Commit automatisch. Es sind keine neuen
   Streamlit-Secrets erforderlich. Bei alter Anzeige die App neu starten.

Workflow 10 läuft täglich zweimal (14:20 und 15:20 UTC), damit Sommer- und Winterzeit
abgedeckt sind. Der zweite Lauf wird durch den eindeutigen Schlüssel zum No-op, wenn
die Entscheidung bereits gespeichert wurde. Pro Tag werden höchstens vier kompakte
Fix-Checkpoint-Entscheidungen erzeugt; es gibt keinen automatischen Backfill.

## Sicherheitsgrenzen

- Production-Neon: ausschließlich explizite `READ ONLY`-Transaktionen;
- Research-Export: maximal 90 Tage beziehungsweise 360 Checkpoint-Zeilen;
- Market-Export: maximal 90 Tage beziehungsweise 5.000 Bucket-Zeilen;
- Live Shadow: nur der aktuelle Madrid-Tag, Settlement-Lookback maximal 14 Tage;
- keine Hourly Arrays, Orderbook-Tiefe oder Provider-Rohdaten im Export;
- AEMET wird nicht aus Neon gelesen und bleibt vom Stored-METAR-Actual und von der
  offiziellen Marktauflösung getrennt;
- `research_only=true`, `automatic_promotion=false`.

Cloudflare-Code, Cron, Bindings und Streamlit-Produktionszugang ändern sich mit
v1.0.11 nicht.
