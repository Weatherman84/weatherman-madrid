import assert from "node:assert/strict";
import test from "node:test";

import {
  aemetFreshness,
  buildAemetDay,
  normalizeAemetObservation,
} from "../src/index.js";

test("AEMET rows are station-scoped and keep decimal temperature", () => {
  assert.equal(normalizeAemetObservation({ idema: "3195", fint: "2026-09-02T16:20:00+0000", ta: 36.2 }), null);
  assert.deepEqual(
    normalizeAemetObservation({
      idema: "3129",
      fint: "2026-09-02T16:20:00+0000",
      ta: "36.2",
      tamax: "36.2",
    }),
    {
      station_id: "3129",
      observed_at: "2026-09-02T16:20:00.000Z",
      temperature_c: 36.2,
      interval_max_c: 36.2,
    },
  );
});

test("AEMET day separates physical Tmax from market resolution", () => {
  const day = buildAemetDay(
    "2026-09-02",
    [
      { station_id: "3129", observed_at: "2026-09-02T14:00:00Z", temperature_c: 35.1, interval_max_c: 35.2 },
      { station_id: "3129", observed_at: "2026-09-02T16:20:00Z", temperature_c: 36.1, interval_max_c: 36.2 },
    ],
    new Date("2026-09-02T16:30:00Z"),
  );

  assert.equal(day.observation_count, 2);
  assert.equal(day.physical_tmax.value_c, 36.2);
  assert.equal(day.physical_tmax.observed_at, "2026-09-02T16:20:00.000Z");
  assert.equal(day.market_resolution_actual, null);
  assert.equal(day.market_resolution_status, "unverified-source-and-rounding-rule");
});

test("AEMET freshness has independent live thresholds", () => {
  const now = new Date("2026-09-02T16:50:00Z");
  assert.equal(aemetFreshness("2026-09-02T16:35:00Z", now).status, "fresh");
  assert.equal(aemetFreshness("2026-09-02T16:20:00Z", now).status, "aging");
  assert.equal(aemetFreshness("2026-09-02T15:50:00Z", now).status, "stale");
});
