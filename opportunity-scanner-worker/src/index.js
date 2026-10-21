const JOURNAL_KEY = "opportunity-scanner-journal.json";
const FEED_KEY = "opportunity-scanner-feed.json";
const RULE_HASH = "b08f9e5be300a46dc82eebd8b5f09d35ae480799ea2ccbd6d5e816b65cbd4420";
const MAX_BYTES = 5_000_000;
const COMMON_MODELS = ["ecmwf_ifs025", "gfs_global", "icon_global"];
const AIRPORTS = ["EDDM", "EHAM", "EPWA", "LEMD", "LTAC", "LTFM"];
const FORBIDDEN_OUTCOME_FIELDS = new Set([
  "actual_bucket", "actual_c", "actual_first_peak_local", "actual_last_peak_local",
  "actual_probability", "brier", "forecast_error_c", "hit", "log_loss",
  "minutes_until_actual_first_peak", "peak_already_observed", "peak_duration_minutes",
]);

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {status, headers: {"content-type": "application/json", "access-control-allow-origin": "*", "cache-control": "no-store"}});
}

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

function validate(payload) {
  const safe = payload?.classification === "CROSS-AIRPORT RESEARCH-ONLY SHADOW JOURNAL"
    && payload?.research_only === true && payload?.automatic_promotion === false
    && payload?.automatic_execution === false && payload?.writes_production_database === false
    && payload?.contains_credentials === false && payload?.automatic_backfill === false
    && payload?.active_rule_count === 24 && payload?.rule_set_sha256 === RULE_HASH;
  if (!safe) throw new Error("journal safety validation failed");
}

function validateFeed(payload) {
  const safe = payload?.schema_version === "opportunity_scanner_feed_v1"
    && payload?.classification === "CROSS-AIRPORT CAUSAL RESEARCH-ONLY SHADOW FEED"
    && payload?.research_only === true && payload?.automatic_promotion === false
    && payload?.automatic_execution === false && payload?.writes_production_database === false
    && payload?.contains_outcomes === false && payload?.automatic_backfill === false
    && stable(payload?.common_models) === stable(COMMON_MODELS)
    && Array.isArray(payload?.forecasts) && payload.forecasts.length === 6;
  if (!safe) throw new Error("feed safety validation failed");
  const airports = [...new Set(payload.forecasts.map(row => row?.airport))].sort();
  if (stable(airports) !== stable(AIRPORTS)) throw new Error("feed must contain the frozen six airports");
  for (const forecast of payload.forecasts) {
    for (const field of FORBIDDEN_OUTCOME_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(forecast, field)) {
        throw new Error(`outcome leakage field forbidden: ${field}`);
      }
    }
  }
}

async function storePayload(env, key, bytes) {
  await env.SCANNER_KV.put(key, new TextDecoder().decode(bytes));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
  return json({status: "published", key, sha256, size_bytes: bytes.byteLength});
}

function assertAppendOnly(previous, next) {
  for (const [collection, key] of [["opportunity_signals", "signal_id"], ["market_snapshots", "market_snapshot_id"], ["shadow_trades", "shadow_trade_id"], ["shadow_resolutions", "shadow_resolution_id"], ["result_observations", "result_observation_id"]]) {
    const incoming = new Map((next[collection] || []).map(row => [row[key], stable(row)]));
    for (const row of previous?.[collection] || []) {
      if (incoming.get(row[key]) !== stable(row)) throw new Error(`immutable ${collection} changed or disappeared: ${row[key]}`);
    }
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === `/${JOURNAL_KEY}`) {
      const value = await env.SCANNER_KV.get(JOURNAL_KEY);
      return value ? new Response(value, {headers: {"content-type": "application/json", "access-control-allow-origin": "*", "cache-control": "public, max-age=60"}}) : json({status: "not_found"}, 404);
    }
    if (request.method === "GET" && url.pathname === `/${FEED_KEY}`) {
      const value = await env.SCANNER_KV.get(FEED_KEY);
      return value ? new Response(value, {headers: {"content-type": "application/json", "access-control-allow-origin": "*", "cache-control": "public, max-age=60"}}) : json({status: "not_found"}, 404);
    }
    if (request.method === "POST" && url.pathname === "/internal/publish-feed") {
      if (!env.PUBLISH_TOKEN || request.headers.get("authorization") !== `Bearer ${env.PUBLISH_TOKEN}`) return json({error: "unauthorized"}, 401);
      const bytes = new Uint8Array(await request.arrayBuffer());
      if (bytes.byteLength > MAX_BYTES) return json({error: "feed_too_large"}, 413);
      try { validateFeed(JSON.parse(new TextDecoder().decode(bytes))); }
      catch (error) { return json({error: String(error.message || error)}, 400); }
      return storePayload(env, FEED_KEY, bytes);
    }
    if (request.method === "POST" && url.pathname === "/internal/publish") {
      if (!env.PUBLISH_TOKEN || request.headers.get("authorization") !== `Bearer ${env.PUBLISH_TOKEN}`) return json({error: "unauthorized"}, 401);
      const bytes = new Uint8Array(await request.arrayBuffer());
      if (bytes.byteLength > MAX_BYTES) return json({error: "journal_too_large"}, 413);
      let payload;
      try { payload = JSON.parse(new TextDecoder().decode(bytes)); validate(payload); }
      catch (error) { return json({error: String(error.message || error)}, 400); }
      const previousText = await env.SCANNER_KV.get(JOURNAL_KEY);
      try { if (previousText) assertAppendOnly(JSON.parse(previousText), payload); }
      catch (error) { return json({error: String(error.message || error)}, 409); }
      return storePayload(env, JOURNAL_KEY, bytes);
    }
    return json({error: "not_found"}, 404);
  },
};
