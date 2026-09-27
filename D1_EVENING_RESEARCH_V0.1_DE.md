# Track C – D−1 Evening Forecast Improvement v0.1

## Zweck

Track C untersucht ausschließlich den fixen Madrid-Checkpoint
`D-1 Evening @20:00`. Stored METAR ist das Forecast-Evaluationsziel; AEMET und
eine bestätigte Market Resolution bleiben getrennte Größen.

## Datenfluss

Der Export liest einmalig nur die jüngsten finalen Madrid-Tage und selektiert:

- den gespeicherten D−1-Checkpoint und die unveränderte Champion-Verteilung;
- pro Modell höchstens den letzten und vorletzten kausal verfügbaren Run;
- kompakte TAF-Revisionsmetadaten ohne Raw-TAF;
- das finale Stored-METAR-Actual.

Die lokale Replay-Analyse arbeitet anschließend ausschließlich mit der
Exportdatei. Ein App-Start löst keinen Export oder Backfill aus.

## Challenger

`d1_evening_challenger_v0.1` nutzt einen expandierenden historischen
Walk-forward-Pool. Am Tag T werden nur finale, `scheduled-causal` Fälle vor T
verwendet. Vor zehn früheren Fällen bleibt die Justierung null. Danach wird die
historische D−1-Restabweichung auf maximal ±0,5 K begrenzt und die gespeicherte
Bucket-Verteilung massenerhaltend verschoben.

Reason Codes umfassen:

- `taf_upside_signal` / `taf_downside_signal`;
- `warming_run_trend` / `cooling_run_trend`;
- `model_split_high`;
- `clear_sky_warm_bias`;
- `negative_anchor_risk`;
- `bucket_boundary_risk`.

Das ist historische Research-Evidenz, kein echtes OOS. `research_only=true` und
`automatic_promotion=false` sind unveränderliche Ausgabefelder.

## Auswertung

Der Export enthält zusätzlich:

- Champion-versus-Challenger-Metriken für Modal Accuracy, Top-2-/Top-3-Coverage,
  MAE, Bias, Brier Score, Log Loss und Kalibrierung;
- getrennte Regime-Matrizen für `scheduled-causal`, `reconstructed_research` und
  alle Research-Fälle;
- eine Fehlfallliste, die nur zum Checkpoint verfügbare Signale als hilfreich oder
  irreführend kennzeichnet;
- eine Verfügbarkeitsübersicht der tatsächlich gefundenen Modellruns und fehlenden
  Felder.

Forecast-Qualität bleibt von einer späteren Trading-Auswertung getrennt. Track C
liest keine Marktpreise und optimiert keine Forecast-Regel anhand hypothetischer P&L.
