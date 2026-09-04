const COLLECTOR_WORKFLOW = "madrid-collector.yml";
const CLOSEOUT_WORKFLOW = "madrid-closeout.yml";
const COLLECTOR_CRON = "7,37 5-20 * * *";
const CLOSEOUT_CRON = "15 19,20 * * *";
const AEMET_CRON = "*/10 * * * *";
const AEMET_STATION_ID = "3129";
const AEMET_STATION_NAME = "Madrid Aeropuerto";
const AEMET_API_URL =
  `https://opendata.aemet.es/opendata/api/observacion/convencional/datos/estacion/${AEMET_STATION_ID}`;
const AEMET_LIVE_KEY = "aemet-live.json";
const AEMET_TODAY_KEY = "aemet-today.json";

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

export function normalizeAemetObservation(row) {
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
      });
      if (normalized) byTimestamp.set(normalized.observed_at, normalized);
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
        observed_at: row.observed_at,
        measurement: row.interval_max_c !== null ? "interval_max_c" : "temperature_c",
      };
    }
  }
  const latest = rows.at(-1) || null;
  return {
    schema_version: "1.0",
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
    status: age <= 20 ? "fresh" : age <= 45 ? "aging" : "stale",
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
    headers: { Accept: "application/json", "User-Agent": "Weatherman-Madrid/1.0.7" },
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
    headers: { Accept: "application/json", "User-Agent": "Weatherman-Madrid/1.0.7" },
  });
  if (!dataResponse.ok) {
    throw new Error(`AEMET data request failed: HTTP ${dataResponse.status}`);
  }
  const payload = await dataResponse.json();
  if (!Array.isArray(payload)) throw new Error("AEMET data response is not a JSON array");
  return payload.map(normalizeAemetObservation).filter(Boolean);
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

async function refreshAemet(env, scheduledTime) {
  if (!env.AEMET_HOT) throw new Error("Missing AEMET_HOT KV binding");
  const attemptedAt = new Date(scheduledTime || Date.now());
  const localDate = madridDate(attemptedAt);
  const previousLive = await env.AEMET_HOT.get(AEMET_LIVE_KEY, "json");
  try {
    const fetched = await fetchAemetObservations(env.AEMET_API_KEY);
    const previousToday = await env.AEMET_HOT.get(AEMET_TODAY_KEY, "json");
    if (previousToday?.local_date && previousToday.local_date !== localDate) {
      await archiveAemetDay(env, previousToday);
    }
    const currentRows = fetched.filter(
      (row) => madridDate(new Date(row.observed_at)) === localDate,
    );
    const retainedRows =
      previousToday?.local_date === localDate ? previousToday.observations : [];
    const today = buildAemetDay(
      localDate,
      mergeObservations(retainedRows, currentRows),
      attemptedAt,
    );
    if (!today.observations.length) {
      throw new Error(`AEMET returned no ${AEMET_STATION_ID} observations for ${localDate}`);
    }
    const latest = today.latest_observation?.observed_at || null;
    const observationsChanged =
      JSON.stringify(previousToday?.observations || []) !==
      JSON.stringify(today.observations);
    if (observationsChanged || previousToday?.local_date !== localDate) {
      await env.AEMET_HOT.put(AEMET_TODAY_KEY, JSON.stringify(today));
    }
    const freshness = aemetFreshness(latest, attemptedAt);
    const live = {
      schema_version: "1.0",
      classification: today.classification,
      station: today.station,
      local_date: localDate,
      latest_observation: today.latest_observation,
      physical_tmax: today.physical_tmax,
      observation_count: today.observation_count,
      freshness_status: freshness.status,
      data_age_minutes: freshness.age_minutes,
      provider_status: "success",
      last_attempt_at: attemptedAt.toISOString(),
      last_successful_fetch_at: attemptedAt.toISOString(),
      last_error: null,
      market_resolution_actual: null,
      market_resolution_status: today.market_resolution_status,
    };
    await env.AEMET_HOT.put(AEMET_LIVE_KEY, JSON.stringify(live));
    return live;
  } catch (error) {
    const latestAt = previousLive?.latest_observation?.observed_at || null;
    const freshness = aemetFreshness(latestAt, attemptedAt);
    const failed = {
      ...(previousLive || {
        schema_version: "1.0",
        classification: "AEMET PHYSICAL OBSERVATIONS — NOT MARKET RESOLUTION",
        station: {
          id: AEMET_STATION_ID,
          name: AEMET_STATION_NAME,
          airport: "LEMD",
          timezone: "Europe/Madrid",
        },
        local_date: localDate,
        latest_observation: null,
        physical_tmax: null,
        observation_count: 0,
        market_resolution_actual: null,
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
    if (request.method !== "GET") {
      return Response.json({ error: "method not allowed" }, { status: 405 });
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
          ...responseHeaders("public, max-age=31536000, immutable"),
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
      },
      { headers: responseHeaders("public, max-age=60") },
    );
  },
};
