# Research Watchlist – v1.0.12

Alle Punkte bleiben `RESEARCH ONLY`. Es folgt daraus keine Änderung an Forecast
Engine, Champion, Biases, TAF-Stufe, Locks oder Promotion.

## Evidenzhygiene

- Scheduled-causal, reconstructed-research und all-research getrennt auswerten.
- Rekonstruierte Checkpoints niemals als historische OOS- oder Live-Shadow-Evidenz
  bezeichnen.
- Vertiefung zunächst für Clear Sky Override, TAF Disagreement, Negative Temperature
  Anchor und Rapid Heat Ramp; N und Checkpoint stets mitführen.

## 14. September 2026

- Late Live traf als einziger Checkpoint Stored METAR 35; Center 34,95 °C.
- Beobachtete Heizrate 2,0 K/h gegenüber modellierten 1,0 K/h bei klarer, trockener
  Atmosphäre; formales Rapid-Heat-Ramp-Signal blieb inaktiv.
- Model Ceiling Reached Early und Phase-vs-Amplitude waren Late Live aktiv.
- AEMET war wegen HTTP 503 nicht verfügbar.
- Nur als Diagnose weiter prüfen, ob die Kombination aus übermodellierter Heizrate,
  klarem Himmel und verbleibender Heizzeit ein eigenes Shadow-Flag verdient.

## 15. September 2026

- Alle vier Modal-Buckets trafen Stored METAR 36; First Live 36,02 °C war numerisch
  am genauesten.
- D0 lag die beobachtete Heizrate mit 3,0 K/h deutlich über dem Modellwert 1,45 K/h,
  wurde aber bereits angemessen verarbeitet. Das zeigt: Heizratendifferenz allein ist
  kein automatischer Temperaturaufschlag.
- TAF TX37 lag einen Bucket zu warm, ohne den richtigen 36er-Modal-Bucket zu verdrängen.
- Analog-Challenger Late Live lag geringfügig näher; rein beobachtend sammeln.
- AEMET war wegen HTTP 503 nicht verfügbar.

Sequenzielle OOS-Evidenz: 15/30 Tage laut Research Handoff vom 15. September. Auch bei
30/30 erfolgt keine automatische Promotion.
