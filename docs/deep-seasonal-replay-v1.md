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

PYTHONPATH=src python scripts/finalize_deep_seasonal_replay.py \
  --root /absolute/path/madrid-deep-seasonal-replay-v1 \
  --archive /absolute/path/madrid-historical-archive-v03 \
  --source /absolute/path/madrid-historical-archive
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

The audited execution uses commit `b442503712bb09e6cb1506ef667d4d28d1b25a09`.
Later formatting and reporting changes preserve the forecast/replay function
ASTs, verified in `deep-seasonal-validation.json`. The original full METAR raw
cache is reused on fresh acquisitions. Its 13,865 forecast observations are
identical to the executed normalized observations; warm evaluation labels are
also identical to v0.3. The finalizer expects the existing Analysis v1 input
manifest and the original source archives alongside the new archive. Fetching
the research branch retains the executed commit for the AST audit.

Findings: 707 target days, 2,828 checkpoints, 685 accepted Actual days and 2,740
evaluable forecasts; 516 days are additional. Modal agreement with v0.3 is only
60--68% by checkpoint. Winter anchor-25% improves accuracy by 1.15 pp but spring
loses 2.23 pp. Seasonal counterfactuals and method drift prohibit treating the
results as a universal production prescription. Only 15 additional market
events were found, starting March 16, 2026; there is no DJF trading validation.
