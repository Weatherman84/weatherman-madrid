# Madrid Late Curve Shadow display — app v1.0.23

The app adds one `v11 Shadow · Late` column directly to each existing checkpoint and bucket table for `madrid_late_single_curve_feature_v1`. There is no separate Shadow panel, chart or history selector. The column remains in the checkpoint table when a current Champion cannot be built. It does not feed any forecast, calibration, decision or trading function. First Live is unchanged. No database schema, query, collector, provider or schedule is added.

The shipped archive holds 434 actual saved research comparisons: 393 cases with the added curve feature and 41 exact TX-reference fallbacks. All were calculated retrospectively. A frozen research case is never labeled as a live forecast. For a selected day without a stored case, the new cells contain `—`, with a short caption explaining the missing Shadow. It never substitutes another day's result as the current target's prediction. The existing target-date control selects which day is displayed.

Only the Late checkpoint row has a Shadow modal bucket and its probability; other checkpoint rows have `—`. The existing bucket table receives the corresponding Shadow probability for each bucket. Extra archived Shadow support is retained in the table union. A caption beneath each table distinguishes retrospective replays, exact reference fallbacks and missing values. Original Champion and replay inputs can differ; the Shadow is explicitly a saved Late forecast rather than the current live nowcast. No live evidence or trade-grade status is claimed.

The runtime reads a checksum-verified JSON archive only. It does not import sklearn/scipy, unpickle estimators, query the database, fetch providers or write forecasts. Integrity failure produces a visible unavailable state without breaking the cockpit. Package-data patterns already include the JSON and SHA-256 files. App/package/bootstrap version is advanced together to 1.0.23 for module-cache consistency; the protected forecast baseline remains v10.7.10.

Regenerate the display data from the immutable supplied study artifacts:

```bash
python scripts/export_madrid_late_shadow_display.py --study /review/inputs/study
```

Run a preview of the same Streamlit component without a database:

```bash
streamlit run scripts/preview_madrid_late_shadow.py
```

The compact review preview uses the same frozen data and table-value functions. It is not a screenshot of the deployed service. Streamlit AppTest covers both column layouts, date changes, missing values and failures. The existing full cockpit test also asserts that both existing tables contain the column and that no additional Shadow panel appears, without extra SQL.

The runtime archive checksum is `7e624832253fe9f97bc129ec352b7b5f1679a32a2d2e5ad4605a2d4cbf662450`. Its predictions come directly from the saved feature experiment and preserve their parameter/source hashes. The export repeats byte-for-byte. No first-run computation or model refit is needed.

The user authorized publishing this compact UI on 23 November 2026. A prospective live recorder and certified input adapter for this candidate are still needed for actual new live Shadow values; this UI change does not activate them. The research training code remains outside this UI release.
