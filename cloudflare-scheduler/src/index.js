const COLLECTOR_WORKFLOW = "madrid-collector.yml";
const CLOSEOUT_WORKFLOW = "madrid-closeout.yml";
const COLLECTOR_CRON = "7,37 5-20 * * *";
const CLOSEOUT_CRON = "15 19,20 * * *";
const AEMET_CRON = "50 * * * *";
const AEMET_STATION_ID = "3129";
const AEMET_STATION_NAME = "Madrid Aeropuerto";
const AEMET_API_URL =
  `https://opendata.aemet.es/opendata/api/observacion/convencional/datos/estacion/${AEMET_STATION_ID}`;
const AEMET_LIVE_KEY = "aemet-live.json";
const AEMET_TODAY_KEY = "aemet-today.json";
const DAILY_ANALYSIS_LATEST_KEY = "daily-analysis-latest.json";
const DAILY_ANALYSIS_META_KEY = "daily-analysis-publication.json";
const DAILY_ANALYSIS_MAX_BYTES = 5 * 1024 * 1024;
const DAILY_ANALYSIS_ARCHIVE_TTL_SECONDS = 90 * 24 * 60 * 60;
const FORWARD_SHADOW_JOURNAL_KEY = "forward-shadow-journal.json";
const FORWARD_SHADOW_META_KEY = "forward-shadow-publication.json";
const FORWARD_SHADOW_MAX_BYTES = 1024 * 1024;

function madridParts(date) {
  return Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Madrid",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(date)
      .map(({ type, value }) => [type, value]),
  );
}

function madridDate(date) {
  const parts = madridParts(date);
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function safeError(error) {
  return String(error?.message || error || "unknown error")
    .replace(/([?&](?:api_?key|token|secret)=)[^&\s]+/gi, "$1REDACTED")
    .slice(0, 500);
}

function timingSafeEqual(left, right) {
  const first = String(left || "");
  const second = String(right || "");
  if (!first.length || first.length !== second.length) return false;
  let difference = 0;
  for (let index = 0; index < first.length; index += 1) {
    difference |= first.charCodeAt(index) ^ second.charCodeAt(index);
  }
  return difference === 0;
}

function lastFinalActualDate(payload) {
  const dates = (Array.isArray(payload?.actuals) ? payload.actuals : [])
    .filter((row) => row?.is_final_station_actual === true)
    .map((row) => String(row?.target_date || ""))
    .filter((value) => /^\d{4}-\d{2}-\d{2}$/.test(value))
    .sort();
  return dates.at(-1) || null;
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function compatibleResolvedOutcome(previous, incoming) {
  if (previous === null) return true;
  if (incoming === null) return false;
  const enrichable = ["aemet_tmax", "resolved_market_bucket"];
  for (const field of enrichable) {
    if (previous[field] !== null && incoming[field] !== previous[field]) return false;
  }
  const priorCore = { ...previous };
  const nextCore = { ...incoming };
  for (const field of enrichable) {
    priorCore[field] = null;
    nextCore[field] = null;
  }
  return JSON.stringify(priorCore) === JSON.stringify(nextCore);
}

export async function publishDailyAnalysis(request, env) {
  if (!env.AEMET_HOT) {
    return Response.json({ error: "AEMET_HOT KV binding is not configured" }, { status: 503 });
  }
  const authorization = request.headers.get("Authorization") || "";
  const suppliedToken = authorization.startsWith("Bearer ")
    ? authorization.slice("Bearer ".length) : "";
  if (!timingSafeEqual(suppliedToken, env.DAILY_ANALYSIS_PUBLISH_TOKEN)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const bytes = await request.arrayBuffer();
  if (!bytes.byteLength || bytes.byteLength > DAILY_ANALYSIS_MAX_BYTES) {
    return Response.json({ error: "invalid export size" }, { status: 413 });
  }
  let payload;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return Response.json({ error: "invalid JSON" }, { status: 400 });
  }
  const targetDate = lastFinalActualDate(payload);
  const valid = payload?.airport === "LEMD"
    && payload?.classification === "READ-ONLY DAILY ANALYSIS EXPORT"
    && payload?.contains_credentials === false
    && payload?.writes_production_database === false
    && payload?.research_only === true
    && typeof payload?.generated_at === "string"
    && targetDate !== null;
  if (!valid) {
    return Response.json({ error: "export safety validation failed" }, { status: 400 });
  }
  const sha256 = await sha256Hex(bytes);
  const publishedAt = new Date().toISOString();
  const metadata = {
    schema_version: "1.0",
    status: "published",
    target_date: targetDate,
    generated_at: payload.generated_at,
    published_at: publishedAt,
    size_bytes: bytes.byteLength,
    sha256,
    latest_key: DAILY_ANALYSIS_LATEST_KEY,
    dated_key: `daily-analysis/${targetDate}.json`,
    research_only: true,
    writes_production_database: false,
  };
  const kvMetadata = {
    content_type: "application/json",
    target_date: targetDate,
    generated_at: payload.generated_at,
    size_bytes: bytes.byteLength,
    sha256,
  };
  await Promise.all([
    env.AEMET_HOT.put(DAILY_ANALYSIS_LATEST_KEY, bytes, { metadata: kvMetadata }),
    env.AEMET_HOT.put(`daily-analysis/${targetDate}.json`, bytes, {
      metadata: kvMetadata,
      expirationTtl: DAILY_ANALYSIS_ARCHIVE_TTL_SECONDS,
    }),
    env.AEMET_HOT.put(DAILY_ANALYSIS_META_KEY, JSON.stringify(metadata)),
  ]);
  console.log(JSON.stringify({ ...metadata, status: "daily-analysis-published" }));
  return Response.json(metadata, { headers: responseHeaders("no-store") });
}

export async function publishForwardShadow(request, env) {
  if (!env.AEMET_HOT) {
    return Response.json({ error: "AEMET_HOT KV binding is not configured" }, { status: 503 });
  }
  const authorization = request.headers.get("Authorization") || "";
  const suppliedToken = authorization.startsWith("Bearer ")
    ? authorization.slice("Bearer ".length) : "";
  if (!timingSafeEqual(suppliedToken, env.DAILY_ANALYSIS_PUBLISH_TOKEN)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const bytes = await request.arrayBuffer();
  if (!bytes.byteLength || bytes.byteLength > FORWARD_SHADOW_MAX_BYTES) {
    return Response.json({ error: "invalid journal size" }, { status: 413 });
  }
  let payload;
  let rawText;
  try {
    rawText = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    payload = JSON.parse(rawText);
  } catch {
    return Response.json({ error: "invalid JSON" }, { status: 400 });
  }
  const decisions = Array.isArray(payload?.decisions) ? payload.decisions : [];
  const valid = payload?.airport === "LEMD"
    && payload?.classification === "READ-ONLY FROZEN FORWARD SHADOW JOURNAL"
    && payload?.contains_credentials === false
    && payload?.writes_production_database === false
    && payload?.research_only === true
    && payload?.automatic_promotion === false
    && typeof payload?.generated_at === "string"
    && decisions.every((row) =>
      typeof row?.decision_id === "string"
      && /^[a-f0-9]{64}$/.test(row?.decision_hash || "")
      && row?.decision_evidence_class === "live_shadow"
      && row?.research_only === true
      && row?.automatic_promotion === false
      && row?.logic_frozen === true
      && (row?.outcome === null
        || (row?.outcome?.outcome_evidence_class === "sequential_oos"
          && Object.hasOwn(row.outcome, "stored_metar_actual")
          && Object.hasOwn(row.outcome, "aemet_tmax")
          && Object.hasOwn(row.outcome, "resolved_market_bucket"))));
  if (!valid) {
    return Response.json({ error: "forward journal safety validation failed" }, { status: 400 });
  }
  const previous = await env.AEMET_HOT.get(FORWARD_SHADOW_JOURNAL_KEY, "json");
  if (previous) {
    const incoming = new Map(decisions.map((row) => [row.decision_id, row]));
    for (const oldRow of Array.isArray(previous.decisions) ? previous.decisions : []) {
      const nextRow = incoming.get(oldRow.decision_id);
      if (!nextRow || nextRow.decision_hash !== oldRow.decision_hash) {
        return Response.json(
          { error: "immutable forward decision changed", decision_id: oldRow.decision_id },
          { status: 409 },
        );
      }
      if (!compatibleResolvedOutcome(oldRow.outcome, nextRow.outcome)) {
        return Response.json(
          { error: "resolved forward outcome changed", decision_id: oldRow.decision_id },
          { status: 409 },
        );
      }
    }
  }
  const sha256 = await sha256Hex(bytes);
  const metadata = {
    schema_version: "1.0",
    status: "published",
    generated_at: payload.generated_at,
    published_at: new Date().toISOString(),
    size_bytes: bytes.byteLength,
    sha256,
    decision_rows: decisions.length,
    resolved_decision_rows: decisions.filter((row) => row.outcome !== null).length,
    research_only: true,
    automatic_promotion: false,
    writes_production_database: false,
  };
  await Promise.all([
    env.AEMET_HOT.put(FORWARD_SHADOW_JOURNAL_KEY, rawText, {
      metadata: { content_type: "application/json", sha256 },
    }),
    env.AEMET_HOT.put(FORWARD_SHADOW_META_KEY, JSON.stringify(metadata)),
  ]);
  console.log(JSON.stringify({ ...metadata, status: "forward-shadow-published" }));
  return Response.json(metadata, { headers: responseHeaders("no-store") });
}

export function normalizeAemetObservation(row, firstSeenAt = null) {
  if (!row || String(row.idema || "") !== AEMET_STATION_ID || !row.fint) {
    return null;
  }
  const observed = new Date(String(row.fint));
  if (Number.isNaN(observed.getTime())) return null;
  const temperature = finiteNumber(row.ta);
  const intervalMaximum = finiteNumber(row.tamax);
  if (temperature === null && intervalMaximum === null) return null;
  return {
    station_id: AEMET_STATION_ID,
    observed_at: observed.toISOString(),
    temperature_c: temperature,
    interval_max_c: intervalMaximum,
    first_seen_at: row.first_seen_at || firstSeenAt,
    first_seen_lag_minutes: (row.first_seen_at || firstSeenAt)
      ? Math.round((new Date(row.first_seen_at || firstSeenAt) - observed) / 6000) / 10 : null,
    // Preserve possible native maximum-time fields, without guessing date/zone semantics.
    maximum_time_fields: row.maximum_time_fields || Object.fromEntries(
      Object.entries(row).filter(([key, value]) =>
        /^(horatamax|htamax|tmax_time|horamax)$/i.test(key) &&
        ["string", "number"].includes(typeof value)),
    ),
    peak_at: null,
    peak_time_verified: false,
    interval_duration_minutes: null,
    interval_semantics: "tamax_provider_interval_not_yet_verified",

  };
}

function mergeObservations(...groups) {
  const byTimestamp = new Map();
  for (const group of groups) {
    for (const row of Array.isArray(group) ? group : []) {
      const normalized = normalizeAemetObservation({
        idema: row.station_id || row.idema,
        fint: row.observed_at || row.fint,
        ta: row.temperature_c ?? row.ta,
        tamax: row.interval_max_c ?? row.tamax,
        first_seen_at: row.first_seen_at,
        maximum_time_fields: row.maximum_time_fields,
      });
      if (normalized) {
        const previous = byTimestamp.get(normalized.observed_at);
        const seen = [previous?.first_seen_at, normalized.first_seen_at].filter(Boolean).sort();
        normalized.first_seen_at = seen[0] || null;
        normalized.first_seen_lag_minutes = seen.length
          ? Math.round((new Date(seen[0]) - new Date(normalized.observed_at)) / 6000) / 10 : null;
        byTimestamp.set(normalized.observed_at, normalized);
      }
    }
  }
  return [...byTimestamp.values()].sort((left, right) =>
    left.observed_at.localeCompare(right.observed_at),
  );
}

export function buildAemetDay(localDate, observations, generatedAt = new Date()) {
  const rows = mergeObservations(observations).filter(
    (row) => madridDate(new Date(row.observed_at)) === localDate,
  );
  let maximum = null;
  for (const row of rows) {
    const value = row.interval_max_c ?? row.temperature_c;
    if (value !== null && (maximum === null || value > maximum.value_c)) {
      maximum = {
        value_c: value,
        observed_at: row.observed_at, // Legacy compatibility: this is report time.
        report_at: row.observed_at,
        peak_at: null,
        peak_time_verified: false,
        maximum_time_fields: row.maximum_time_fields,
        time_role: "report_time_not_exact_peak",
        interval_duration_minutes: null,
        measurement: row.interval_max_c !== null ? "interval_max_c" : "temperature_c",
      };
    }
  }
  const latest = rows.at(-1) || null;
  return {
    schema_version: "1.1",
    classification: "AEMET PHYSICAL OBSERVATIONS — NOT MARKET RESOLUTION",
    station: {
      id: AEMET_STATION_ID,
      name: AEMET_STATION_NAME,
      airport: "LEMD",
      timezone: "Europe/Madrid",
    },
    local_date: localDate,
    generated_at: generatedAt.toISOString(),
    observation_count: rows.length,
    public_series_cadence_minutes: 60,
    interval_semantics: "tamax_provider_interval_not_yet_verified",
    first_seen_note: "First detected by this Worker; polling delay is included, not exact publication time.",
    latest_observation: latest,
    physical_tmax: maximum,
    observations: rows,
    market_resolution_actual: null,
    market_resolution_status: "unverified-source-and-rounding-rule",
  };
}

export function aemetFreshness(observedAt, now = new Date()) {
  if (!observedAt) return { status: "stale", age_minutes: null };
  const observed = new Date(observedAt);
  if (Number.isNaN(observed.getTime())) return { status: "stale", age_minutes: null };
  const age = Math.max(0, (now.getTime() - observed.getTime()) / 60000);
  return {
    status: age <= 75 ? "current" : age <= 120 ? "delayed" : "stale",
    age_minutes: Math.round(age * 10) / 10,
  };
}

async function dispatchWorkflow(env, workflow, scheduledSlot, collectionMode) {
  const owner = env.GITHUB_OWNER || "weatherman84";
  const repository = env.GITHUB_REPO || "weatherman-madrid";
  const reference = env.GITHUB_REF || "main";
  if (!env.GITHUB_TOKEN) throw new Error("Missing required GITHUB_TOKEN secret");

  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repository}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "Content-Type": "application/json",
        "User-Agent": "weatherman-madrid-cloudflare-scheduler",
        "X-GitHub-Api-Version": "2026-03-10",
      },
      body: JSON.stringify({
        ref: reference,
        inputs: {
          scheduled_slot: scheduledSlot,
          source: "cloudflare",
          ...(collectionMode ? { collection_mode: collectionMode } : {}),
        },
      }),
    },
  );
  if (response.status !== 204) {
    const detail = await response.text();
    throw new Error(`GitHub dispatch failed for ${workflow}: HTTP ${response.status} ${detail}`);
  }
}

async function fetchAemetObservations(apiKey) {
  if (!apiKey) throw new Error("Missing required AEMET_API_KEY secret");
  const endpoint = new URL(AEMET_API_URL);
  endpoint.searchParams.set("api_key", apiKey);
  const metadataResponse = await fetch(endpoint, {
    headers: { Accept: "application/json", "User-Agent": "Weatherman-Madrid/1.0.13" },
  });
  if (!metadataResponse.ok) {
    throw new Error(`AEMET metadata request failed: HTTP ${metadataResponse.status}`);
  }
  const metadata = await metadataResponse.json();
  if (!metadata?.datos || (metadata.estado && Number(metadata.estado) !== 200)) {
    throw new Error(`AEMET metadata response unavailable: estado ${metadata?.estado || "unknown"}`);
  }
  const dataUrl = new URL(String(metadata.datos));
  if (
    dataUrl.protocol !== "https:" ||
    !(dataUrl.hostname === "aemet.es" || dataUrl.hostname.endsWith(".aemet.es"))
  ) {
    throw new Error("AEMET returned an unexpected data host");
  }
  const dataResponse = await fetch(dataUrl, {
    headers: { Accept: "application/json", "User-Agent": "Weatherman-Madrid/1.0.13" },
  });
  if (!dataResponse.ok) {
    throw new Error(`AEMET data request failed: HTTP ${dataResponse.status}`);
  }
  const payload = await dataResponse.json();
  if (!Array.isArray(payload)) throw new Error("AEMET data response is not a JSON array");
  const firstSeenAt = new Date().toISOString();
  return mergeObservations(payload.map((row) => normalizeAemetObservation(row, firstSeenAt)).filter(Boolean));
}

async function gzipJson(payload) {
  const input = new Blob([JSON.stringify(payload)]).stream();
  const compressed = input.pipeThrough(new CompressionStream("gzip"));
  return new Response(compressed).arrayBuffer();
}

async function archiveAemetDay(env, payload) {
  if (!payload?.local_date || !payload?.observations?.length) return;
  const [year, month, day] = payload.local_date.split("-");
  const key = `archive/aemet/${year}/${month}/${day}.json.gz`;
  await env.AEMET_HOT.put(key, await gzipJson(payload), {
    metadata: {
      content_type: "application/json",
      content_encoding: "gzip",
      local_date: payload.local_date,
    },
  });
}

function archiveKey(localDate) {
  return `archive/aemet/${localDate.replaceAll("-", "/")}.json.gz`;
}

async function readArchive(env, localDate) {
  const value = await env.AEMET_HOT.get(archiveKey(localDate), "arrayBuffer");
  if (value === null) return null;
  const stream = new Blob([value]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).json();
}

export async function refreshAemet(env, scheduledTime) {
  if (!env.AEMET_HOT) throw new Error("Missing AEMET_HOT KV binding");
  const attemptedAt = new Date();
  const localDate = madridDate(attemptedAt);
  const previousLive = await env.AEMET_HOT.get(AEMET_LIVE_KEY, "json");
  try {
    const fetched = await fetchAemetObservations(env.AEMET_API_KEY);
    if (!fetched.length) throw new Error("AEMET returned no usable 3129 observations");
    const completedAt = new Date();
    const previousToday = await env.AEMET_HOT.get(AEMET_TODAY_KEY, "json");
    // Archive all earlier days present in the provider response. Merge late values
    // with the existing gzip archive; never overwrite a full day with a partial tail.
    const olderDays = new Set(fetched.map((row) => madridDate(new Date(row.observed_at)))
      .filter((day) => day < localDate));
    if (previousToday?.local_date < localDate) olderDays.add(previousToday.local_date);
    const archiveErrors = [];
    for (const day of olderDays) {
      try {
        const prior = await readArchive(env, day);
        const retained = previousToday?.local_date === day ? previousToday.observations : [];
        const incoming = fetched.filter((row) => madridDate(new Date(row.observed_at)) === day);
        const archive = buildAemetDay(day, mergeObservations(prior?.observations, retained, incoming), completedAt);
        if (JSON.stringify(prior?.observations || []) !== JSON.stringify(archive.observations)) {
          await archiveAemetDay(env, archive);
        }
      } catch (error) {
        archiveErrors.push({ local_date: day, error: safeError(error) });
      }
    }
    const retained = previousToday?.local_date === localDate ? previousToday.observations : [];
    const today = buildAemetDay(localDate, mergeObservations(retained, fetched), completedAt);
    if (JSON.stringify(previousToday?.observations || []) !== JSON.stringify(today.observations)
        || previousToday?.local_date !== localDate) {
      await env.AEMET_HOT.put(AEMET_TODAY_KEY, JSON.stringify(today));
    }
    const latest = today.latest_observation || fetched.at(-1);
    const freshness = aemetFreshness(latest?.observed_at, completedAt);
    const live = {
      schema_version: "1.1",
      classification: today.classification,
      station: today.station,
      local_date: localDate,
      latest_observation: latest,
      physical_tmax: today.physical_tmax,
      observation_count: today.observation_count,
      public_series_cadence_minutes: 60,
      freshness_status: freshness.status,
      data_age_minutes: freshness.age_minutes,
      provider_status: "success",
      archive_status: archiveErrors.length ? "partial_failure" : "success",
      archive_errors: archiveErrors,
      scheduled_slot: new Date(scheduledTime || attemptedAt).toISOString(),
      last_attempt_at: attemptedAt.toISOString(),
      last_successful_fetch_at: completedAt.toISOString(),
      last_error: null,
      market_resolution_actual: null,
      market_resolution_status: today.market_resolution_status,
    };
    await env.AEMET_HOT.put(AEMET_LIVE_KEY, JSON.stringify(live));
    return live;
  } catch (error) {
    const freshness = aemetFreshness(previousLive?.latest_observation?.observed_at, new Date());
    const failed = {
      ...(previousLive || {
        schema_version: "1.1",
        classification: "AEMET PHYSICAL OBSERVATIONS — NOT MARKET RESOLUTION",
        station: { id: AEMET_STATION_ID, name: AEMET_STATION_NAME,
          airport: "LEMD", timezone: "Europe/Madrid" },
        local_date: localDate, latest_observation: null, physical_tmax: null,
        observation_count: 0, market_resolution_actual: null,
        last_successful_fetch_at: null,
        market_resolution_status: "unverified-source-and-rounding-rule",
      }),
      freshness_status: freshness.status,
      data_age_minutes: freshness.age_minutes,
      provider_status: "failed",
      last_attempt_at: attemptedAt.toISOString(),
      last_error: safeError(error),
    };
    await env.AEMET_HOT.put(AEMET_LIVE_KEY, JSON.stringify(failed));
    throw error;
  }
}

function responseHeaders(cacheControl = "public, max-age=60") {
  return {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": cacheControl,
    "Content-Type": "application/json; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
  };
}

async function kvJsonResponse(env, key, cacheControl) {
  if (!env.AEMET_HOT) {
    return Response.json({ error: "AEMET_HOT KV binding is not configured" }, { status: 503 });
  }
  const value = await env.AEMET_HOT.get(key, "text");
  if (value === null) return Response.json({ error: "not found" }, { status: 404 });
  return new Response(value, { headers: responseHeaders(cacheControl) });
}

async function dailyAnalysisResponse(request, env, key, cacheControl) {
  if (!env.AEMET_HOT) {
    return Response.json({ error: "AEMET_HOT KV binding is not configured" }, { status: 503 });
  }
  const result = await env.AEMET_HOT.getWithMetadata(key, "arrayBuffer");
  if (result.value === null) return Response.json({ error: "not found" }, { status: 404 });
  const metadata = result.metadata || {};
  const headers = {
    ...responseHeaders(cacheControl),
    ...(metadata.sha256 ? { ETag: `"${metadata.sha256}"` } : {}),
    ...(metadata.target_date ? { "X-Export-Target-Date": metadata.target_date } : {}),
    ...(metadata.generated_at ? { "X-Export-Generated-At": metadata.generated_at } : {}),
    ...(metadata.size_bytes ? { "X-Export-Size": String(metadata.size_bytes) } : {}),
  };
  if (request.method === "HEAD") return new Response(null, { headers });
  return new Response(result.value, { headers });
}

export default {
  async scheduled(controller, env, ctx) {
    const scheduled = new Date(controller.scheduledTime);
    const scheduledSlot = scheduled.toISOString();
    const clock = madridParts(scheduled);

    if (controller.cron === AEMET_CRON) {
      ctx.waitUntil(
        refreshAemet(env, controller.scheduledTime)
          .then((live) =>
            console.log(
              JSON.stringify({
                status: "aemet-stored",
                observed_at: live.latest_observation?.observed_at || null,
                physical_tmax_c: live.physical_tmax?.value_c || null,
              }),
            ),
          )
          .catch((error) => console.error(`AEMET refresh failed: ${safeError(error)}`)),
      );
      return;
    }

    if (controller.cron === CLOSEOUT_CRON) {
      if (clock.hour !== "21" || clock.minute !== "15") {
        console.log(
          JSON.stringify({
            status: "dst-companion-skipped",
            cron: controller.cron,
            scheduled_slot: scheduledSlot,
            madrid_time: `${clock.hour}:${clock.minute}`,
          }),
        );
        return;
      }
      ctx.waitUntil(
        dispatchWorkflow(env, CLOSEOUT_WORKFLOW, scheduledSlot).then(() =>
          console.log(
            JSON.stringify({
              status: "dispatched",
              workflow: CLOSEOUT_WORKFLOW,
              scheduled_slot: scheduledSlot,
            }),
          ),
        ),
      );
      return;
    }

    if (controller.cron !== COLLECTOR_CRON) {
      console.warn(JSON.stringify({ status: "unknown-cron-skipped", cron: controller.cron }));
      return;
    }
    const fixedHours = new Set(["09", "12", "16", "20"]);
    const collectionMode =
      clock.minute === "07" && fixedHours.has(clock.hour) ? "fixed" : "aviation";
    ctx.waitUntil(
      dispatchWorkflow(env, COLLECTOR_WORKFLOW, scheduledSlot, collectionMode).then(() =>
        console.log(
          JSON.stringify({
            status: "dispatched",
            workflow: COLLECTOR_WORKFLOW,
            scheduled_slot: scheduledSlot,
            collection_mode: collectionMode,
          }),
        ),
      ),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/internal/publish-daily-analysis") {
      return publishDailyAnalysis(request, env);
    }
    if (request.method === "POST" && url.pathname === "/internal/publish-forward-shadow") {
      return publishForwardShadow(request, env);
    }
    if (!["GET", "HEAD"].includes(request.method)) {
      return Response.json({ error: "method not allowed" }, { status: 405 });
    }
    if (url.pathname === "/daily-analysis-latest.json") {
      return dailyAnalysisResponse(
        request, env, DAILY_ANALYSIS_LATEST_KEY, "public, max-age=60"
      );
    }
    if (url.pathname === `/${FORWARD_SHADOW_JOURNAL_KEY}`) {
      return kvJsonResponse(env, FORWARD_SHADOW_JOURNAL_KEY, "public, max-age=60");
    }
    if (url.pathname === `/${FORWARD_SHADOW_META_KEY}`) {
      return kvJsonResponse(env, FORWARD_SHADOW_META_KEY, "public, max-age=60");
    }
    if (/^\/daily-analysis\/\d{4}-\d{2}-\d{2}\.json$/.test(url.pathname)) {
      return dailyAnalysisResponse(
        request, env, url.pathname.slice(1), "public, max-age=300"
      );
    }
    if (url.pathname === `/${DAILY_ANALYSIS_META_KEY}`) {
      return kvJsonResponse(env, DAILY_ANALYSIS_META_KEY, "public, max-age=60");
    }
    if (url.pathname === "/aemet-live.json") {
      return kvJsonResponse(env, AEMET_LIVE_KEY, "public, max-age=60");
    }
    if (url.pathname === "/aemet-today.json") {
      return kvJsonResponse(env, AEMET_TODAY_KEY, "public, max-age=60");
    }
    if (/^\/archive\/aemet\/\d{4}\/\d{2}\/\d{2}\.json\.gz$/.test(url.pathname)) {
      if (!env.AEMET_HOT) {
        return Response.json({ error: "AEMET_HOT KV binding is not configured" }, { status: 503 });
      }
      const value = await env.AEMET_HOT.get(url.pathname.slice(1), "arrayBuffer");
      if (value === null) return Response.json({ error: "not found" }, { status: 404 });
      return new Response(value, {
        headers: {
          ...responseHeaders("public, max-age=300"),
          "Content-Encoding": "gzip",
        },
      });
    }
    return Response.json(
      {
        service: "Weatherman Madrid scheduler and AEMET observation cache",
        status: "ready",
        dispatches_data: false,
        collector_cron_utc: COLLECTOR_CRON,
        closeout_cron_utc: CLOSEOUT_CRON,
        aemet_cron_utc: AEMET_CRON,
        aemet_station: AEMET_STATION_ID,
        aemet_hot_store_configured: Boolean(env.AEMET_HOT),
        aemet_key_configured: Boolean(env.AEMET_API_KEY),
        daily_analysis_mirror_configured: Boolean(
          env.AEMET_HOT && env.DAILY_ANALYSIS_PUBLISH_TOKEN
        ),
      },
      { headers: responseHeaders("public, max-age=60") },
    );
  },
};
