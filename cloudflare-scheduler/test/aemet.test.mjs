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
      first_seen_at: null, first_seen_lag_minutes: null,
      maximum_time_fields: {}, peak_at: null, peak_time_verified: false,
      interval_duration_minutes: null, interval_semantics: "tamax_provider_interval_not_yet_verified",
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
  for (const [minutes, expected] of [[75, "current"], [75.1, "delayed"], [120, "delayed"], [120.1, "stale"]]) {
    assert.equal(aemetFreshness(new Date(now - minutes * 60000).toISOString(), now).status, expected);
  }
});

import worker, { refreshAemet } from "../src/index.js";
import { readFile } from "node:fs/promises";

class KV {
  values = new Map();
  writes = [];
  async get(key, type) {
    const value = this.values.get(key);
    if (value === undefined) return null;
    return type === "json" ? JSON.parse(value) : value;
  }
  async put(key, value) { this.values.set(key, value); this.writes.push(key); }
}

async function decode(value) {
  return new Response(new Blob([value]).stream().pipeThrough(new DecompressionStream("gzip"))).json();
}

function fixedClock(t, iso) {
  t.mock.timers.enable({ apis: ["Date"], now: new Date(iso).getTime() });
}
function provider(t, getRows) {
  t.mock.method(globalThis, "fetch", async (url) => {
    if (String(url).includes("/api/")) {
      return Response.json({ estado: 200, datos: "https://opendata.aemet.es/test-data" });
    }
    return Response.json(getRows());
  });
}

const row = (time, temp = 37.6) => ({ idema: "3129", fint: time, ta: temp, tamax: temp });

test("first_seen survives duplicate fetch and native peak field is preserved without guessing", async (t) => {
  fixedClock(t, "2026-09-06T18:50:00Z");
  provider(t, () => [{ ...row("2026-09-06T18:00:00Z"), horatamax: "17:46" }]);
  const env = { AEMET_HOT: new KV(), AEMET_API_KEY: "test" };
  await refreshAemet(env, Date.now());
  const first = await env.AEMET_HOT.get("aemet-today.json", "json");
  assert.equal(first.latest_observation.first_seen_at, "2026-09-06T18:50:00.000Z");
  assert.equal(first.latest_observation.first_seen_lag_minutes, 50);
  assert.deepEqual(first.physical_tmax.maximum_time_fields, { horatamax: "17:46" });
  assert.equal(first.physical_tmax.peak_at, null);
  t.mock.timers.tick(3600000);
  const live = await refreshAemet(env, Date.now());
  const again = await env.AEMET_HOT.get("aemet-today.json", "json");
  assert.equal(again.latest_observation.first_seen_at, first.latest_observation.first_seen_at);
  assert.equal(live.last_successful_fetch_at, "2026-09-06T19:50:00.000Z");
  assert.equal(live.provider_status, "success");
  assert.equal(live.freshness_status, "delayed");
  assert.equal(env.AEMET_HOT.writes.filter((key) => key === "aemet-today.json").length, 1);
});

test("midnight accepts previous-day latest observation and repairs archive from late arrivals", async (t) => {
  fixedClock(t, "2026-09-06T22:50:00Z"); // 00:50 Madrid Sep 7
  let incoming = [row("2026-09-06T20:00:00Z", 36)];
  provider(t, () => incoming);
  const env = { AEMET_HOT: new KV(), AEMET_API_KEY: "test" };
  const old = buildAemetDay("2026-09-06", [normalizeAemetObservation(row("2026-09-06T14:00:00Z", 38))]);
  await env.AEMET_HOT.put("aemet-today.json", JSON.stringify(old));
  const live = await refreshAemet(env, Date.now());
  assert.equal(live.provider_status, "success");
  assert.equal(live.physical_tmax, null);
  const today = await env.AEMET_HOT.get("aemet-today.json", "json");
  assert.equal(today.local_date, "2026-09-07");
  assert.equal(today.observation_count, 0);
  incoming = [row("2026-09-06T21:00:00Z", 39), row("2026-09-06T22:00:00Z", 35)];
  t.mock.timers.tick(3600000);
  await refreshAemet(env, Date.now());
  const archive = await decode(await env.AEMET_HOT.get("archive/aemet/2026/09/06.json.gz", "arrayBuffer"));
  assert.equal(archive.observation_count, 3);
  assert.equal(archive.physical_tmax.value_c, 39);
  assert.equal((await env.AEMET_HOT.get("aemet-today.json", "json")).physical_tmax.value_c, 35);
});

test("provider failure preserves last success and observation independently", async (t) => {
  fixedClock(t, "2026-09-06T18:50:00Z");
  provider(t, () => [row("2026-09-06T18:00:00Z")]);
  const env = { AEMET_HOT: new KV(), AEMET_API_KEY: "test" };
  const success = await refreshAemet(env, Date.now());
  t.mock.method(globalThis, "fetch", async () => { throw new Error("timeout"); });
  t.mock.timers.tick(3600000);
  await assert.rejects(refreshAemet(env, Date.now()), /timeout/);
  const failed = await env.AEMET_HOT.get("aemet-live.json", "json");
  assert.equal(failed.last_successful_fetch_at, success.last_successful_fetch_at);
  assert.equal(failed.latest_observation.temperature_c, 37.6);
  assert.equal(failed.provider_status, "failed");
  assert.equal(failed.freshness_status, "delayed");
});

test("unknown and removed ten-minute cron never dispatch the collector", async (t) => {
  const dispatch = t.mock.method(globalThis, "fetch", async () => { throw new Error("must not fetch"); });
  for (const cron of ["*/10 * * * *", "unknown"]) {
    await worker.scheduled({ cron, scheduledTime: Date.now() }, {}, {
      waitUntil() { assert.fail("unknown cron must not schedule work"); },
    });
  }
  assert.equal(dispatch.mock.callCount(), 0);
});

test("configured hourly cron runs AEMET only; collector and closeout still dispatch", async (t) => {
  fixedClock(t, "2026-09-06T18:50:00Z");
  const config = JSON.parse(await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"));
  assert.deepEqual(config.triggers.crons, ["50 * * * *", "7,37 5-20 * * *", "15 19,20 * * *"]);
  let calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push(String(url));
    if (String(url).includes("github.com")) {
      assert.ok(JSON.parse(options.body).inputs.scheduled_slot);
      return new Response(null, { status: 204 });
    }
    return String(url).includes("/api/")
      ? Response.json({ estado: 200, datos: "https://opendata.aemet.es/test-data" })
      : Response.json([row("2026-09-06T18:00:00Z")]);
  });
  const pending = [];
  const env = { AEMET_HOT: new KV(), AEMET_API_KEY: "test", GITHUB_TOKEN: "test" };
  await worker.scheduled({ cron: "50 * * * *", scheduledTime: Date.now() }, env,
                         { waitUntil(p) { pending.push(p); } });
  await Promise.all(pending);
  assert.equal(calls.filter((url) => url.includes("github.com")).length, 0);
  calls = []; pending.length = 0;
  for (const [cron, time] of [["7,37 5-20 * * *", "2026-09-06T07:07:00Z"],
                             ["15 19,20 * * *", "2026-09-06T19:15:00Z"]]) {
    await worker.scheduled({ cron, scheduledTime: Date.parse(time) }, env,
                           { waitUntil(p) { pending.push(p); } });
  }
  await Promise.all(pending);
  assert.equal(calls.length, 2);
  assert.ok(calls.some((url) => url.includes("madrid-closeout.yml")));
});

test("archive write failure does not hide a successful provider fetch", async (t) => {
  fixedClock(t, "2026-09-07T12:50:00Z");
  provider(t, () => [row("2026-09-06T20:00:00Z"), row("2026-09-07T12:00:00Z")]);
  const kv = new KV();
  const originalPut = kv.put.bind(kv);
  kv.put = async (key, value) => {
    if (key.startsWith("archive/")) throw new Error("KV archive failure");
    await originalPut(key, value);
  };
  const result = await refreshAemet({ AEMET_HOT: kv, AEMET_API_KEY: "test" }, Date.now());
  assert.equal(result.provider_status, "success");
  assert.equal(result.archive_status, "partial_failure");
  assert.equal(result.archive_errors[0].local_date, "2026-09-06");
});
