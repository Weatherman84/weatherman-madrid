# Research Watchlist – Madrid v1.0.18

Diese Watchlist ist rein additiv. Sämtliche bis v1.0.13 dokumentierten Tagesbefunde
bleiben gültig; v1.0.18 übernimmt sie als Research-Kontext und ergänzt die Fälle vom
26. und 27. September 2026. Kein Punkt verändert die produktive Forecast Engine.

## Übergreifende Hypothesen aus allen bisherigen Handoffs

- Champion-Center, Center-Bucket und Modal-Bucket getrennt bewerten.
- Stored METAR, AEMET Physical Tmax und Market Resolution strikt trennen.
- Plateau, mehrfacher Bucket-Touch und einzelner Upper-Bucket-Touch separat erfassen.
- Positive TAF-Evidenz nicht pauschal verstärken; negative Upper-Tail-Dämpfung nur
  kontextabhängig und ausschließlich als Challenger untersuchen.
- TAF-Revisionen, TAF-vs-Modell-Konflikte und erforderliche Heizrate bis zur
  TAF-Peakzeit als getrennte kausale Features behandeln.
- Clear Sky, Cloud Surprise, Radiation, Late Dry Mixing, Heating Rate, Anchor,
  Persistent Hot und Regional Cluster auf überlappende Evidenz prüfen.
- Model Ceiling Reached Early und kurzfristige Temperaturrückgänge nicht automatisch
  als Peak-Ende interpretieren; verbleibende Strahlung und spätere Rückkehr zum
  Maximum bleiben Gegenbeispiele.
- Analog Memory, Airport Anchor Transfer und weitere Challenger ausschließlich
  checkpointweise auswerten und nicht vorzeitig promoten.
- Top-1-/Top-2-Abstand, Modellfrische, fehlende Modelle, TAF-TX-Verfügbarkeit und
  Providerfehler als Unsicherheits- beziehungsweise Datenqualitätsmerkmale führen.

## 26. September 2026

- First Live traf den 34er-Bucket; Late Live überzeichnete auf 35.
- Großer Live-Uplift von rund +1,776 K vor TAF; TX35 erzeugte anschließend einen
  falschen Modal-Flip von 34 auf 35.
- Clear Sky, Late Dry Mixing, Temperaturanker, Cloud Surprise und Radiation auf
  Overlap prüfen.
- Einzelner 34er-Touch über längerem 33er-Plateau getrennt von stabilem Peakplateau
  kalibrieren.
- Analog Challenger war numerisch an allen Checkpoints näher, bleibt aber Shadow.

## 27. September 2026

- Nur D−1 traf den finalen 31er-Bucket. Late Live führte 33; 31 erhielt nur 11,74 %.
- Neun Late-Live-Modelle lagen zwischen 29,2 und 31,6 °C, während TAF TX34 meldete.
- Live erhöhte den Bias-Center um +1,550 K; Clear Sky, Late Dry Mixing, Heating Rate,
  Cloud Surprise und Anchor wirkten gleichzeitig positiv.
- Kausal prüfen: erforderlicher Anstieg bis zur TAF-Peakzeit, bereits erreichtes
  Maximum, Plateau, Winddrehung und TAF-vs-Modell-Differenz.
- Marktdivergenz nur anhand kausaler Bid-/Ask-Snapshots, Spread, Preisalter und später
  bestätigter Market Resolution untersuchen.
- AEMET 31,3 °C bleibt eine getrennte physische Intervallgröße mit unverifizierter
  Intervallsemantik und keinem automatischen Market-Actual-Status.

## Schutzstatus

- Forecast Engine v10.7.11; geschützte Baseline v10.7.10.
- `research_only=true`; `automatic_promotion=false`.
- Keine Änderung von Champion, Biases, TAF-Stufe, Locks oder produktiven Regimes.
- Keine Promotion vor ausreichender sequenzieller OOS-/Live-Shadow-Evidenz.
