import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

class KV {
  constructor() { this.data = new Map(); }
  async get(key) { return this.data.get(key) ?? null; }
  async put(key, value) { this.data.set(key, value); }
}

const base = {
  classification: "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL", research_only: true,
  automatic_promotion: false, automatic_execution: false, writes_production_database: false,
  contains_credentials: false, automatic_backfill: false, active_rule_count: 24,
  rule_set_sha256: "b08f9e5be300a46dc82eebd8b5f09d35ae480799ea2ccbd6d5e816b65cbd4420",
  opportunity_signals: [], market_snapshots: [], shadow_trades: [], shadow_resolutions: [],
};

const feed = {
  schema_version: "opportunity_scanner_feed_v1",
  classification: "CROSS-AIRPORT CAUSAL RESEARCH-ONLY SHADOW FEED",
  research_only: true, automatic_promotion: false, automatic_execution: false,
  writes_production_database: false, contains_outcomes: false, automatic_backfill: false,
  common_models: ["ecmwf_ifs025", "gfs_global", "icon_global"],
  forecasts: ["LEMD", "EHAM", "EDDM", "EPWA", "LTAC", "LTFM"].map(airport => ({airport})),
  markets: [], peak_history: {},
};

test("publishes and serves only authenticated safe journal", async () => {
  const env = {SCANNER_KV: new KV(), PUBLISH_TOKEN: "secret"};
  const denied = await worker.fetch(new Request("https://x/internal/publish", {method: "POST", body: JSON.stringify(base)}), env);
  assert.equal(denied.status, 401);
  const posted = await worker.fetch(new Request("https://x/internal/publish", {method: "POST", headers: {authorization: "Bearer secret"}, body: JSON.stringify(base)}), env);
  assert.equal(posted.status, 200);
  const fetched = await worker.fetch(new Request("https://x/opportunity-scanner-journal.json"), env);
  assert.equal(fetched.status, 200);
});

test("rejects mutation of an immutable signal", async () => {
  const kv = new KV();
  const env = {SCANNER_KV: kv, PUBLISH_TOKEN: "secret"};
  const first = {...base, opportunity_signals: [{signal_id: "one", value: 1}]};
  const second = {...base, opportunity_signals: [{signal_id: "one", value: 2}]};
  const request = body => new Request("https://x/internal/publish", {method: "POST", headers: {authorization: "Bearer secret"}, body: JSON.stringify(body)});
  assert.equal((await worker.fetch(request(first), env)).status, 200);
  assert.equal((await worker.fetch(request(second), env)).status, 409);
});

test("publishes and serves a safe causal feed", async () => {
  const env = {SCANNER_KV: new KV(), PUBLISH_TOKEN: "secret"};
  const denied = await worker.fetch(new Request("https://x/internal/publish-feed", {method: "POST", body: JSON.stringify(feed)}), env);
  assert.equal(denied.status, 401);
  const posted = await worker.fetch(new Request("https://x/internal/publish-feed", {method: "POST", headers: {authorization: "Bearer secret"}, body: JSON.stringify(feed)}), env);
  assert.equal(posted.status, 200);
  const fetched = await worker.fetch(new Request("https://x/opportunity-scanner-feed.json"), env);
  assert.equal(fetched.status, 200);
  assert.equal((await fetched.json()).contains_outcomes, false);
});

test("rejects outcome leakage in a feed", async () => {
  const env = {SCANNER_KV: new KV(), PUBLISH_TOKEN: "secret"};
  const unsafe = {...feed, forecasts: feed.forecasts.map((row, index) => index ? row : {...row, actual_bucket: 20})};
  const posted = await worker.fetch(new Request("https://x/internal/publish-feed", {method: "POST", headers: {authorization: "Bearer secret"}, body: JSON.stringify(unsafe)}), env);
  assert.equal(posted.status, 400);
});
