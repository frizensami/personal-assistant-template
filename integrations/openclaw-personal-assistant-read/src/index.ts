import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { readActiveTasks, readScheduledReminders, readStatus, type ClientConfig } from "./client.js";

const StoreSecretRefSchema = Type.Object(
  {
    source: Type.Literal("store"),
    provider: Type.Literal("default"),
    id: Type.String({ minLength: 1 }),
  },
  { additionalProperties: false },
);

const ConfigSchema = Type.Object(
  {
    baseUrl: Type.String({
      minLength: 1,
      description: "Personal Assistant API origin. HTTPS is required except for loopback HTTP.",
    }),
    token: Type.Union([
      Type.String({
        minLength: 1,
        description: "Bearer token for the Personal Assistant read API.",
      }),
      StoreSecretRefSchema,
    ]),
    proxyUrl: Type.Optional(
      Type.String({
        minLength: 1,
        description: "Optional loopback HTTP proxy used to reach a userspace Tailscale client.",
      }),
    ),
  },
  { additionalProperties: false },
);

const NoParameters = Type.Object({}, { additionalProperties: false });

export default defineToolPlugin({
  id: "openclaw-personal-assistant-read",
  name: "Personal Assistant Read",
  description: "Read status, active self tasks, and ordinary scheduled reminders from a Personal Assistant API.",
  configSchema: ConfigSchema,
  tools: (tool) => [
    tool({
      name: "personal_assistant_status",
      label: "Personal Assistant Status",
      description: "Read the Personal Assistant service status. Performs only GET /internal/v1/status.",
      parameters: NoParameters,
      execute: async (_params, config, context) => readStatus(config as ClientConfig, context.signal),
    }),
    tool({
      name: "personal_assistant_active_tasks",
      label: "Personal Assistant Active Tasks",
      description: "Read active self tasks. Performs only GET /internal/v1/tasks.",
      parameters: NoParameters,
      execute: async (_params, config, context) => readActiveTasks(config as ClientConfig, context.signal),
    }),
    tool({
      name: "personal_assistant_scheduled_reminders",
      label: "Personal Assistant Scheduled Reminders",
      description: "Read ordinary scheduled reminders. Performs only GET /internal/v1/reminders.",
      parameters: NoParameters,
      execute: async (_params, config, context) => readScheduledReminders(config as ClientConfig, context.signal),
    }),
  ],
});
