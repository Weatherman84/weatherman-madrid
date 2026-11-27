# Madrid capture and display repair – app 1.0.26

This release implements the operational recommendation from the 27 November
review. No model promotion, automatic execution, parameter tuning or historical
backfill is included. All 30 protected forecast-baseline files remain identical.

## Existing tables

The fixed-checkpoint and bucket tables split v11 into Reference, Curve and
Fallback columns. Curve is empty when unavailable; the emitted Fallback equals
Reference exactly. First Live receives no v11 values. The bucket table also
selects the stored Champion for exactly that day's 16:00 checkpoint, independently
of the current Champion nowcast. A missing or ambiguous stored pair stays empty.

Captions name the actual capture time, source version, parameter hash, source-
specific curve/fallback counts and fallback reason. Historical replay remains
labelled retrospective. Missing 24/27 November recordings have dated operational
incident explanations, never fabricated forecasts. Future rejected attempts carry
their own measured reason and timestamps in the append-only data branch.

## Capture diagnostics

Known input rejections have safe structured codes (missing model, stale receipt,
stale cycle, timestamp order, stale METAR). The recorder writes an operational
attempt and refreshes the journal even after an input/SQL rejection. The workflow
publishes these diagnostics before ending visibly as failed. Operational attempts
never enter forecast, outcome or OOS counts. Existing decisions/outcomes cannot
be replaced. Unexpected software failures remain failures, not synthetic gaps.

## Separate input contract v2

`live_open_meteo_coherent_day_v2` requests one combined daily/hourly temperature
response for each of the six existing models, for exactly the Europe/Madrid day.
Coordinates remain the protected LEMD configuration's 40.466/-3.555. Unix timestamps
must match every actual UTC instant of that local day (including DST day lengths).
There is no old-run substitution, reanalysis, stitching or late retrieval.

Metadata run/availability must remain stable around each response; raw response
text, request parameters, receipt timestamps and metadata are frozen in the
decision's input bundle. The request/validation contract has its own hash and is
revalidated when reading a decision. This archives a current provider product;
it does not certify that the provider internally serves an isolated model run.
Two bounded fetch workers avoid a large request burst. Failure leaves a measured
gap, rather than using unverified old data.

Model parameters and feature formulas are unchanged. The daily/hourly maximum
equality check is NOT relaxed. A disagreement yields the unchanged reference
fallback and is reported explicitly. The new source version has separate evidence
counts under `evidence_by_input_source_class`; old v1 decisions remain untouched.

**Measured limitation:** the after-cutoff public-source diagnostic on 27 November
returned a complete 24-hour local day for all six models. Five daily maxima differed
from their hourly maxima by 0.1 C. These observations are in
`research/madrid_capture_repair_v1/source-probe-2026-11-27.json`; they are not OOS
forecasts and do not prove a working active-curve case. The v2 adapter deliberately
retains the fallback for such a case. A future active curve still requires all
frozen checks to pass before cutoff. Changing the maximum definition/tolerance
requires a separate research version and evaluation, not this repair.

## Morning and fixed-checkpoint preparation

The workflow wraps the unchanged collector. On the existing 08:37, 11:37, 15:37
and 19:37 Madrid slots, it prepares current provider forecasts before the next
checkpoint. It invokes only the existing source writer, not decision/market/
Actual/backfill paths. Up to two attempts are allowed; retries stop five minutes
before cutoff. Committed source receipts are checked and retained as an Actions
artifact. The normal collector runs afterwards. Provider parallelism is bounded
to four workers, with a ten-second timeout.

After a checkpoint run, the wrapper verifies a unique stored scheduled-causal
ForecastSnapshot ID, rather than treating a successful collector slot as evidence
of a forecast. A missing/ambiguous snapshot makes the Actions job fail visibly.
The protected collection-run implementation still retains its own unchanged
status semantics. The Daily export therefore separately reports actual checkpoint
recording health; a slot-coverage score is not a recording score.

These are normal operational source-table writes once the workflow is active.
Development verification used synthetic/local databases and public API requests;
it made no production database calls. No schema changes are required. The separate
v11 source reader retains its verified read-only repeatable transaction.

## Analog and export evidence

First/Late remain separate. Existing numerical gate conditions and labels are
unchanged; an additional provenance review reports exact matching snapshot times,
reconstructed/unknown recordings and late model receipts. It never authorizes a
release or treats provider availability as proven reception. Only already loaded
snapshot rows are used; evidence outside the loaded window stays unknown.

The daily export includes a separate v11 journal section after its SQL session
has rolled back, plus actual recording-health metadata. Journal/network failure
is explicit; no replay data substitute for missing live records. This export
change does not add production SQL queries.

## Operational acceptance still required

- Observe actual subsequent Morning snapshot IDs and pre-cutoff receipt artifacts.
- Observe real v2 pre-cutoff records and rejection reasons, not merely green audits.
- Require a genuinely active curve case before claiming the curve is working.
- Keep v1/v2, curve/fallback and First/Late evidence separate; no automatic promotion.
- A new model release still requires at least 30 genuine sequential OOS days and
  quality criteria; two legacy fallback cases do not satisfy that requirement.
