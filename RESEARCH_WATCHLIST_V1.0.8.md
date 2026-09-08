# Research Handoff übernommen – v1.0.8

Basis: vom Nutzer gemeldete Tagesanalyse zu v1.0.7. Kein unabhängiger Live-Backcheck.

- Stored METAR 37 °C, AEMET 37,6 °C, Serienlücke +0,6 K. Kein neuer Bias aus einem Tag.
- Alle Checkpoints erkannten den Stored-METAR-Bucket; Late Live nahe dem Stored Maximum,
  D0 näher am physischen AEMET-Maximum. Zielgrößen streng getrennt bewerten.
- TCU/CB-Reste, danach Aufklaren; gemeldetes 37er-Plateau 16:30–19:00 LT.
  Die gemeldete physische Peakzeit 18:00 LT bleibt bis zur Feldprüfung Berichtszeit.
- Flache 30-Minuten-Rate um 16 Uhr verhinderte späteren Sprung 36→37 nicht.
- TAF TX36 unterschätzte beide Serien, TAF-Stufe blieb neutral. TAF bleibt eigene Stufe.
- Kein Rapid-Heat-Ramp-Signal. Physical Stall und METAR Persistence bleiben
  RESEARCH ONLY, insufficient_oos_data, probability null, champion_impact_c 0.0.
- Beobachten: Post-Convective-Spread, D0 4/9 und First Live 8/9 Modelle,
  Regime Memory 6/30 OOS-Tage, ein METAR-/TAF-Timeout, zwei partielle 429-Läufe.
- Gemeldete Pipeline-Coverage 100 % und erfolgreicher Closeout: bestehende Kadenz erhalten.
- Sechs fehlende AEMET-Archive als fehlend behandeln; keine synthetische Rekonstruktion.
- Keine Challenger-Promotion ohne mindestens 30 echte sequenzielle OOS-Tage.
  Madrid-Anchor, Champion-Gewichte, Biaswerte, Locks und produktive Regimegewichte unverändert.
