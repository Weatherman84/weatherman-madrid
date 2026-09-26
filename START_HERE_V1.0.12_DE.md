# Weatherman Madrid v1.0.12 installieren

Dieses kleine Folgeupdate zu v1.0.11 trennt Research-Evidenz sauber. Die produktive
Forecast Engine bleibt v10.7.11, die geschützte fachliche Baseline v10.7.10.

## Installation ab installiertem v1.0.11

1. Den Inhalt des v1.0.12-Updatepakets in den bestehenden GitHub-Branch `main`
   hochladen und vorhandene Dateien ersetzen.
2. Auch die mitgelieferten Dateien unter `.github/workflows` hochladen. Die
   Workflow-Nummern bleiben unverändert.
3. Unter **Actions → 0 - Tests** den erfolgreichen Lauf abwarten.
4. Unter **Actions → 4 - Prepare isolated replay lab → Run workflow** einmal starten.
   Dieser Schritt ergänzt in der isolierten Replay-Datenbank ausschließlich:
   - `checkpoint_status`
   - `checkpoint_reconstructed`
   Bereits unter v1.0.11 gespeicherte Entscheidungen bleiben unveränderlich; die neuen
   Provenienzfelder sind dort `null` und damit ausdrücklich unbekannt. Sie werden nicht
   nachträglich als Scheduled-Evidenz angenommen.
5. Unter **Actions → 9 - Export Madrid research tracks → Run workflow** zuerst
   `dry_run=true` und anschließend bei Bedarf `dry_run=false` mit `days=30` ausführen.
   Der neue Export trägt `schema_version: "1.1"`.
6. Streamlit übernimmt v1.0.12 automatisch. Falls noch v1.0.11 angezeigt wird, die
   App in Streamlit einmal neu starten.

Workflow 4 schreibt nur in die mit `REPLAY_DATABASE_URL` konfigurierte isolierte
Replay-Datenbank. Production-Neon wird weiterhin ausschließlich read-only gelesen.
Cloudflare, Cron und Bindings ändern sich nicht.

## Prüfpunkte im neuen Export

- Jede Trading-Decision enthält `checkpoint_status`, `checkpoint_reconstructed`
  und `evidence_class`.
- Rekonstruierte Checkpoints tragen `evidence_class: "reconstructed_research"`.
- `regime_research.matrix_default_view` ist `scheduled_causal_only`.
- `regime_research.matrix_views` enthält:
  - `scheduled_causal_only`
  - `reconstructed_research`
  - `all_research_evidence`
- `research_only` ist `true`; `automatic_promotion` ist `false`.

Beispiel (gekürzt):

```json
{
  "schema_version": "1.1",
  "trading_challenger": {
    "decisions": [{
      "checkpoint": "Late Live @16:00",
      "checkpoint_status": "reconstructed-causal",
      "checkpoint_reconstructed": true,
      "evidence_class": "reconstructed_research"
    }]
  },
  "regime_research": {
    "matrix_default_view": "scheduled_causal_only",
    "matrix_views": {
      "scheduled_causal_only": {"checkpoint_records": 0, "matrix": []},
      "reconstructed_research": {"checkpoint_records": 1, "matrix": []},
      "all_research_evidence": {"checkpoint_records": 1, "matrix": []}
    }
  }
}
```

## Sicherheitsgrenzen

Die bestehenden Grenzen bleiben unverändert: maximal 90 Exporttage, 360
Checkpoint-Zeilen und 5.000 Market-Zeilen. Kein automatischer Backfill und keine
zusätzliche Neon-Abfrage beim Rendern der Research-UI. Der Dry-Run schätzt für 30
Tage nun konservativ ungefähr 264.000 Byte Exportdatei; er führt weiterhin null
Production-Abfragen aus.
