# Research Watchlist – v1.0.11

Stand der übernommenen Madrid-Handoffs: sequenzielle OOS-Evidenz 13/30.
Alles bleibt `RESEARCH ONLY`; Replay ist keine OOS-Evidenz.

## Forecast- und Zieltrennung

- Champion-Center, Center-Bucket und gespeicherten Champion-Modal-Bucket getrennt
  bewerten; Modal-Bucket nie rückwirkend aus dem Center ableiten.
- Stored METAR, AEMET Physical Tmax und bestätigte Market Resolution niemals mischen.
- AEMET–METAR-Abstände weiter als mögliche Stations-, Sensor-, Zeit- oder
  Verarbeitungsdifferenz beobachten, nicht als kalibrierten Bias behandeln.

## Live-, TAF- und Regimefälle

- TAF-TX versus frühem Modal-Bucket, besonders bei knappen Top-1-/Top-2-Abständen,
  bestätigtem Rapid Heat Ramp und klarem/trockenem Verlauf auswerten.
- Späte einzelne Bucket-Touches nach längerem Plateau und wiederholte Bucket-Touches
  getrennt erfassen.
- Negative Temperaturanker während laufender Erwärmung sowie positive Late-Live-Anker
  bei stagnierender METAR-Reihe prüfen.
- Clear Sky Override, Late Dry Mixing, Phase Anchor, Failed Convection,
  Post-Convective Spread, Regional Cluster, Persistent Hot und Maritime Advection nur
  mit explizitem N und checkpointbezogen untersuchen.
- Analog-Challenger Late Live und TAF-Mehrwert parallel gegen Stored METAR und AEMET
  auswerten; keine vorzeitige Promotion.

## Datenqualität und Betrieb

- Modellabdeckung und Modellausschlüsse zwischen Checkpoints, insbesondere AROME,
  dokumentieren.
- Open-Meteo-429, METAR-/TAF-Timeouts, Collector-Lücken und AEMET-Archivstatus weiter
  beobachten.
- Regime Memory ohne Monatsreset fortführen; produktive Promotion frühestens nach
  mindestens 30 echten sequenziellen OOS-Tagen.

## Neue v0.1-Tracks

- `trading_challenger_v0.1` ab Installation unverändert vorwärts protokollieren.
- First-Live-TAF-Hedge von 30 % und Late-Live-Top-2-Regel als Hypothese behandeln,
  nicht als kalibrierte Portfolioempfehlung.
- Market-Freshness-Bänder getrennt auswerten: ≤60, ≤180, ≤360 und >360 Minuten nur
  als Sensitivität.
- `regime_research_matrix_v0.1` zunächst ohne Multi-Regime-Kombinationen; Kombinationen
  erst ab N≥10 und weiterhin mit Small-Sample-Warnung.

Unverändert bleiben Madrid-Anchor, Champion-Gewichte, Biaswerte, produktive Regimes,
TAF-Stufe, Day-/Peak-Lock, Forecast-Baseline v10.7.10 und sämtliche OOS-/Promotion-Gates.
