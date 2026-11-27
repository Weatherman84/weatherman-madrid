# Engine v11 — isolated offline research v0.1

**NO RELEASE CANDIDATE.** No production imports, DB access, runtime wiring, deployment or order execution.

- [Data manifest](../../V11_DATA_MANIFEST.md)
- [Bake-off report and decision](../../V11_MODEL_BAKEOFF.md)
- [Future OOS gates](OOS_GATES.json)
- [Reliability plot](artifacts/reliability.png)

| Deliverable | Location | Status |
|---|---|---|
| Immutable source audit | `artifacts/archive_sources.json`, `input_manifest.json`, `extended_manifest.json` | Supplied archives inspected and hashed |
| Versioned features | `artifacts/features.json` | 23 explicit features; missingness preserved |
| Main six-model comparison | `artifacts/results.json`, `rows.jsonl.gz` | Discovery, calibration and retrospective validation separated |
| Paired results on identical days | `artifacts/paired_comparisons.jsonl.gz` | 845 comparisons; no mixed evidence cohorts |
| Eight-model comparison including AROME/HD | `artifacts/extended_results.json`, `extended_rows.jsonl.gz` | Separate April–October experiment |
| Per-day probability vectors and scores | `artifacts/predictions.jsonl.gz`, `extended_predictions.jsonl.gz`, `scores.csv.gz` | Machine-readable, lossless compression |
| Model parameters and local fitted state | `artifacts/model_config.json`, `fitted_models.joblib` | Includes imputation, scaling, coefficients, weights and input fingerprint |
| Mandatory diagnosis cases | `artifacts/case_registry.json` | All 41 calendar days, missing original summaries explicit |
| 17 November counterexample | `artifacts/counterexample_2026-11-17.json` | Reconstructed, provisional, positive probability on later bucket |
| Market proxy comparison | `artifacts/market_proxy.json` | Only 47 matched cases; no executable quotes or ROI |
| Live probability freeze / separate market adapter | `src/weatherman/v11/shadow.py` | Local write-once snapshot; always research-only |
| Optional discrete hazard | `src/weatherman/v11/contracts.py` | Aggregation contract tested; no fitted hazard model claimed |
| Future sequential OOS | `OOS_GATES.json` | Not started; zero prospective v11 days |

The main model is fixed to Madrid and a six-model common set. Other airports are inventoried only. The optional scientific dependencies are local to this research workflow; production requirements and existing calibration from PR #30 are unchanged. Case summaries copied from PR #31 introduce no code dependency on that PR.

Run commands and exact limitations are in the bake-off report. Never deserialize an untrusted joblib/pickle file.
