# Weatherman Madrid v1.0.15 – D−1 Evening Research

v1.0.15 baut direkt auf v1.0.14 auf. Forecast Engine v10.7.11 und die
geschützte Forecast-Baseline v10.7.10 bleiben unverändert.

## Installation

1. Den Release-Branch nach erfolgreichem GitHub-Testlauf in `main` mergen.
2. Streamlit Community Cloud übernimmt `main` automatisch; anschließend im
   Cockpit `App v1.0.15` prüfen.
3. Für Track C unter **Actions** den Workflow
   **11 - Export Madrid D-1 evening research** zunächst mit `dry_run=true`
   starten. Dieser Lauf fragt Neon nicht ab.
4. Nur wenn die ausgegebenen Grenzen passen, denselben Workflow einmal mit
   `dry_run=false` und `days=30` starten. Das Ergebnis liegt 14 Tage als
   Artifact `madrid-d1-evening-research-v0.1` bereit.

## Schutzgrenzen

- Production Neon: explizite Read-only-Transaktion mit anschließendem Rollback;
- höchstens 90 finale Tage; SQL-Zeilenlimits skalieren zusätzlich mit dem gewählten
  Zeitraum (bei 30 Tagen: 30 D−1-Checkpoints, 1.440 Modellzeilen und 240
  TAF-Metadatenzeilen);
- keine Hourly Arrays, Modell-Grids oder Raw-TAFs;
- kein automatischer Backfill;
- AEMET und Market Resolution werden nicht aus Neon als Forecast-Actual gelesen;
- `research_only=true`, `automatic_promotion=false`.

## Interpretation

Der Challenger ist ein historischer, chronologischer Walk-forward-Researchlauf.
Er ist kein sequenzielles OOS-Ergebnis und keine Trading-Empfehlung. Erst nach
einem eingefrorenen Shadow-Start dürfen neue Tage als `live_shadow` bewertet
werden.
