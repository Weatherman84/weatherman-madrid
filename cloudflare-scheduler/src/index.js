const COLLECTOR_WORKFLOW = "madrid-collector.yml";
const CLOSEOUT_WORKFLOW = "madrid-closeout.yml";
const CLOSEOUT_CRON = "15 19,20 * * *";

function madridClock(date) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Madrid",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  return Object.fromEntries(parts.map(({ type, value }) => [type, value]));
}

async function dispatchWorkflow(env, workflow, scheduledSlot) {
  const owner = env.GITHUB_OWNER || "weatherman84";
  const repository = env.GITHUB_REPO || "weatherman-madrid";
  const reference = env.GITHUB_REF || "main";
  if (!env.GITHUB_TOKEN) {
    throw new Error("Missing required GITHUB_TOKEN secret");
  }

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
        },
      }),
    },
  );
  if (response.status !== 204) {
    const detail = await response.text();
    throw new Error(
      `GitHub dispatch failed for ${workflow}: HTTP ${response.status} ${detail}`,
    );
  }
}

export default {
  async scheduled(controller, env, ctx) {
    const scheduled = new Date(controller.scheduledTime);
    const scheduledSlot = scheduled.toISOString();
    const clock = madridClock(scheduled);
    const isCloseoutTrigger = controller.cron === CLOSEOUT_CRON;

    if (isCloseoutTrigger) {
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

    ctx.waitUntil(
      dispatchWorkflow(env, COLLECTOR_WORKFLOW, scheduledSlot).then(() =>
        console.log(
          JSON.stringify({
            status: "dispatched",
            workflow: COLLECTOR_WORKFLOW,
            scheduled_slot: scheduledSlot,
          }),
        ),
      ),
    );
  },

  async fetch() {
    return Response.json({
      service: "Weatherman Madrid scheduler",
      status: "ready",
      dispatches_data: false,
      collector_cron_utc: "7,22,37,52 5-20 * * *",
      closeout_cron_utc: CLOSEOUT_CRON,
    });
  },
};
