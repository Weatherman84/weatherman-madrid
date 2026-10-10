# Deep Seasonal Historical Replay v1

Research-only extension of Madrid v0.3. Separate cohort:
`deep_seasonal_reduced_fidelity`, November 2024--October 2026. Original v0.3
remains `historical_partial_fidelity_2026`; it is not full-fidelity OOS.

```bash
PYTHONPATH=src python scripts/deep_seasonal_replay.py \
  --root /absolute/path/madrid-deep-seasonal-replay-v1 \
  --archive /absolute/path/madrid-historical-archive-v03 \
  --source /absolute/path/madrid-historical-archive --mode acquire

PYTHONPATH=src python scripts/historical_market_prices.py \
  --archive /absolute/path/madrid-deep-seasonal-replay-v1 \
  --start 2024-11-01 --end 2026-03-31 --max-requests 1600

PYTHONPATH=src python scripts/deep_seasonal_replay.py \
  --root /absolute/path/madrid-deep-seasonal-replay-v1 \
  --archive /absolute/path/madrid-historical-archive-v03 \
  --source /absolute/path/madrid-historical-archive --mode replay --workers 8

PYTHONPATH=src python scripts/deep_seasonal_replay.py \
  --root /absolute/path/madrid-deep-seasonal-replay-v1 \
  --archive /absolute/path/madrid-historical-archive-v03 \
  --source /absolute/path/madrid-historical-archive --mode analyze
```

Six existing models use Previous Runs hourly 48h/24h forecasts. A complete
local day is required, including 23/25-hour DST days. D-1 normally uses 48h;
D0/Live normally uses 24h. Each contributing run time is bounded by
valid time minus fixed lead; estimated availability is the latest bound plus
6h10m. Initialization remains unknown, with no invented run age. All data are
filtered to availability at or before checkpoint. This is conditional causal
research, not certified historical receipt evidence.

A private preparation adapter handles the unknown initialization and a private
daily fixed-lead cadence evaluates this deliberate daily research series. The
production freshness function and engine files remain untouched. Thus this
cannot reproduce the precise current production Champion in winter. The overlap
comparison measures the joint effect of model set, lead, cadence, missing 850hPa
and weather variables, rather than attributing all drift to model count.

Only existing temperature-anchor and late-dry-mixing contributions are weighted.
The same private engine hooks as Analysis v1 apply weights before guards and
interactions. Current=100%; no fitted parameter family, new regime or promotion.
Earlier accepted Actuals train existing station/model bias using fixed-lead
forecast pairs, never reanalysis. Current/future labels are attached after
forecasting. Run-trend information is unavailable, not inferred from different
forecast leads. Memory shadow enrichment is explicitly unavailable.

Existing trading helpers preserve probability-weighted dollar stake baskets,
market settlement winners, causal prices, and missing-leg rejection. Archived
price histories are gross `price_based_hypothetical_pnl`, not executable fills.
Single NO-TRADE gates are tested independently; prior poor-regime performance
uses only earlier Actuals available by the checkpoint. No future prices or labels
train gates. No clean untouched holdout or annual trading edge is certified.

Source requests use content-addressed caches. Model, METAR and TAF normalization,
all replay inputs and code are fingerprinted. Replay and analysis require no
production services or Neon reads. All cohorts, checkpoints and freshness caps
remain separate in results.
