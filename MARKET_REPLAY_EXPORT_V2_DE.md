# Market Replay Export v2 – Madrid

## Zweck und Sicherheitsgrenzen

`market-replay-export.json` stellt gespeicherte Polymarket-Marktdaten für den
Trading-Challenger im Replay-Chat bereit. Der Export ist ausschließlich
`research_only`; er verändert weder Forecasts noch Datenbankinhalte.

- Airport: nur LEMD/Madrid
- Standardumfang: 30 finale Stored-METAR-Tage
- Checkpoints: D−1 20:00, D0 09:00, First Live 12:00, Late Live 16:00
- Neon Production: explizite read-only Transaktion mit abschließendem Rollback
- keine Polymarket-Liveabfrage während des Exports
- keine Orderbook-Tiefe, keine Forecast-JSON-Blobs, keine vollständige
  Intraday-Markthistorie
- `research_only: true`; `automatic_promotion: false`

## Export in GitHub erzeugen

1. Repository `weatherman84/weatherman-madrid` öffnen.
2. **Actions** wählen.
3. Workflow **8 - Export Madrid market replay** öffnen.
4. **Run workflow** anklicken.
5. Zuerst `dry_run=true` lassen. Danach erneut starten, `dry_run=false` setzen,
   `days=30` lassen und `end_date` normalerweise leer lassen.
6. Nach erfolgreichem Lauf unten unter **Artifacts**
   `market-replay-export` herunterladen.
7. ZIP entpacken und `market-replay-export.json` in den Replay-Chat hochladen.

Es ist kein neues Secret erforderlich. Der Workflow verwendet das bereits
vorhandene GitHub-Secret `DATABASE_URL`. Den Wert niemals in Dateien, Logs oder
den Chat kopieren.

## Auswahl- und Kausalitätsregel

Für jeden Zieltag und Checkpoint wird in Neon ausschließlich der jüngste
gespeicherte Snapshot ausgewählt, dessen `captured_at` kleiner oder gleich
`checkpoint_at` ist. Ein zeitlich näherer Snapshot nach dem Checkpoint wird nie
verwendet.

- identischer gespeicherter Zeitstempel:
  `exact_checkpoint_market_snapshot`
- früherer gespeicherter Zeitstempel:
  `nearest_available_before_checkpoint`
- kein kausal verfügbarer Zeitstempel:
  `unavailable_no_causal_market_snapshot`

`market_snapshot_age_minutes` zeigt den zeitlichen Abstand. Für historische
Trade-Price-Samples bedeutet „exact“ den gespeicherten Sample-Zeitpunkt. Es ist
keine Garantie, dass zu dieser Sekunde ein ausführbarer Exchange-Ask existierte.
Deshalb bleiben `best_bid`, `best_ask`, `spread` und `liquidity` bei solchen
Samples leer und `price_kind` ausdrücklich sichtbar.

## Beispielstruktur

```json
{
  "schema_version": "1.0",
  "application_version": "1.0.11",
  "export_engine_version": "v10.7.11",
  "protected_forecast_baseline": "v10.7.10",
  "airport": "LEMD",
  "timezone": "Europe/Madrid",
  "requested_final_days": 30,
  "research_only": true,
  "automatic_promotion": false,
  "checkpoints": [
    {
      "target_date": "2026-09-13",
      "checkpoint_label": "First Live @12:00",
      "checkpoint_at": "2026-09-13T10:00:00+00:00",
      "status": "available",
      "provenance": "nearest_available_before_checkpoint",
      "market_captured_at": "2026-09-13T09:57:00+00:00",
      "market_snapshot_age_minutes": 3.0,
      "markets": [
        {
          "bucket_label": "33°C",
          "bucket_c": 33,
          "yes_price": 0.42,
          "best_bid": 0.41,
          "best_ask": 0.43,
          "spread": 0.02,
          "volume": 1240.0,
          "liquidity": 380.0,
          "closed": false,
          "yes_won": null,
          "resolution_source": "LEMD METAR",
          "price_kind": "live"
        }
      ]
    }
  ]
}
```

## Lokaler Aufruf für Entwickler

```bash
python scripts/export_market_replay.py --days 30 --output market-replay-export.json
```

Ohne Neon-Abfrage lassen sich Grenzen und Größe vorher schätzen:

```bash
python scripts/export_market_replay.py --days 30 --dry-run
```

Optional kann der Zeitraum reproduzierbar begrenzt werden:

```bash
python scripts/export_market_replay.py --days 30 --end-date 2026-09-13
```
