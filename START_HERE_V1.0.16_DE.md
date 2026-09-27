# Weatherman Madrid v1.0.16 – Track-C-Export-Hotfix

v1.0.16 baut direkt auf v1.0.15 auf. Forecast Engine v10.7.11 und die
geschützte Forecast-Baseline v10.7.10 bleiben unverändert.

## Installation

1. Den Release-Branch nach erfolgreichem GitHub-Testlauf in `main` mergen.
2. Streamlit Community Cloud übernimmt `main` automatisch; anschließend im
   Cockpit `App v1.0.16` prüfen.
3. Unter **Actions** den Workflow **11 - Export Madrid D-1 evening research**
   mit `days=30` und `dry_run=false` genau einmal starten.
4. Nach erfolgreichem Lauf das Artifact `madrid-d1-evening-research-v0.1`
   herunterladen. Es enthält `d1-evening-replay-export.json` und ein Manifest.

Der Dry Run von v1.0.15 wurde bereits erfolgreich ausgeführt. Der erste reale Lauf
wurde von der Zeilenschutzgrenze gestoppt und hat weder ein Artifact noch einen
Production-Write erzeugt. Ein weiterer Dry Run ist deshalb optional.

## Was der Hotfix ändert

- Jede Zieldatum-Abfrage verwendet ihr eigenes kausales 48-Stunden-Fenster vor
  dem gespeicherten D−1-Checkpoint.
- Mehrere Collector-Kopien desselben Modelllaufs werden in SQL dedupliziert.
- Je Modell werden höchstens die zwei jüngsten unterschiedlichen Läufe gelesen.
- Für 30 Tage werden höchstens 720 kompakte Modellzeilen übertragen; hinzu kommen
  höchstens 30 Checkpointzeilen und 240 TAF-Metadatenzeilen.

## Unveränderte Schutzregeln

- Production Neon läuft in einer expliziten Read-only-Transaktion mit Rollback.
- Kein automatischer Backfill und kein Export beim Öffnen der App.
- Keine Hourly Arrays, Modell-Grids, Raw-TAFs oder großen JSON-Blobs.
- `research_only=true`, `automatic_promotion=false`.
- Stored METAR, AEMET und Market Resolution bleiben getrennte Zielreihen.

Der Export ist historisches Research und kein sequenzielles OOS-Ergebnis oder eine
Trading-Empfehlung.
