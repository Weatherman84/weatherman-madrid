# Historical Replay Analysis v1

Offline research over the immutable Madrid v0.3 archive. Production engine,
Champion, regime definitions, public causal trainer guards and live trading
recommendations remain unchanged. `research_only=true`,
`automatic_promotion=false`; no certified OOS claim.

```bash
PYTHONPATH=src python scripts/historical_replay_analysis.py \
  --archive /absolute/path/madrid-historical-archive-v03 \
  --source /absolute/path/madrid-historical-archive \
  --output /absolute/path/madrid-historical-analysis-v1 \
  --workers 8
```

Install `requirements-historical-replay.txt` first. `--mode cross` generates
checkpoint forecasts for all six archive assumptions; `--mode analyze` consumes
the frozen local cross-check cache. `--dates` limits a diagnostic verification
run and adds uncached engine-prefix identity checks. It does not define a new
holdout. Full runs also require exact identity with the archived v0.3 main
Champion at every checkpoint.

The process prohibits internet sockets, reads only local normalized archive
files, fingerprints all inputs and forecast code, checks source immutability,
and resumes completed days from fingerprinted caches. Output must be outside
both immutable source archives and the git checkout. The frozen v0.3 memory
snapshot projection is included in the analysis input bundle; changed policies
do not rewrite prior memory or terminal decisions.

## Interpretation

The main cohort is `diagnostic_partial_replay`, complete accepted Actuals only.
Its original `evidence_class=historical_partial_fidelity` is preserved. Other
requested evidence cohorts are reported separately with N=0 when absent.

Existing contribution weights are rerun through the existing engine before
guards and interactions. Exact calibration-prefix reuse is scoped to one
checkpoint/input scenario, with copied return values; zero-effect reuse depends
on raw contributions before guards. D0/Late legacy challengers are separately
labelled as their original additive approximation. Diagnostic D−1 adaptations
reuse existing training formulas without modifying public scheduled-causal
guards or relabelling rows. Training uses earlier target dates and Actual
receipt timestamps, separately for expanding and rolling 90-day windows.

All April–October history was previously inspected:
`no_clean_untouched_historical_holdout_available`. Monthly blocked validation
and temporal parameter selection are diagnostic. The in-sample upper bound is
the best tested existing one-factor setup, not a mathematical global optimum;
the separately labelled casewise Actual oracle cannot be deployed.

Trading is `price_based_hypothetical_pnl`, using cached Polymarket price-history
points and resolved market metadata, never assumed METAR settlement. One unit
total stake per checkpoint, gross returns, no verified bid/ask, fills, fees,
slippage or liquidity. All basket legs must have causal prices within the
specified freshness cap. Curves assume target-day ordering and a 1000-unit
initial bankroll; actual settlement times and capital lock are unknown.

The requested Probability Net is unavailable if no existing implementation is
found. Observable scalar NO TRADE gates are labelled as a partial diagnostic
subset; the full quote-dependent production filter cannot be certified.

Master outputs: regime tournament (including combinations), policy/sensitivity
matrix, existing parameter study, walk-forward tournament, challenger
tournament, historical trading tournament, summary JSON and readable Markdown
report. Edge-band and NO TRADE JSON plus forecast/trade record caches support
independent auditing. Rankings never trigger promotion.
