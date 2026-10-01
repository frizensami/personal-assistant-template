import { createServer } from "node:http";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { getToolPluginMetadata } from "openclaw/plugin-sdk/tool-plugin";
import {
  API_PATHS,
  readActiveTasks,
  readScheduledReminders,
  readStatus,
  requestJson,
  validateBaseUrl,
  validateProxyUrl,
} from "./client.js";
import entry from "./index.js";

const seen: Array<{ method?: string; url?: string; authorization?: string }> = [];
const server = createServer((request, response) => {
  seen.push({
    method: request.method,
    url: request.url,
    authorization: request.headers.authorization,
  });
  response.writeHead(200, { "content-type": "application/json" });
  response.end(JSON.stringify({ path: request.url }));
});

let baseUrl: string;
const token = "test-token-that-must-not-appear-in-errors";

beforeAll(async () => {
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Test server did not bind a TCP port.");
  baseUrl = `http://127.0.0.1:${address.port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
});

describe("plugin metadata", () => {
  it("declares only the three read tools with closed, empty parameter schemas", () => {
    const tools = getToolPluginMetadata(entry)?.tools;
    expect(tools?.map((tool) => tool.name)).toEqual([
      "personal_assistant_status",
      "personal_assistant_active_tasks",
      "personal_assistant_scheduled_reminders",
    ]);
    for (const tool of tools ?? []) {
      expect(tool.parameters).toMatchObject({ type: "object", additionalProperties: false });
      expect(tool.parameters.properties ?? {}).toEqual({});
    }
  });
});

describe("fixed read client", () => {
  it("uses GET, a fixed route, JSON accept, and the bearer token for each operation", async () => {
    seen.length = 0;
    const config = { baseUrl, token };
    await expect(readStatus(config)).resolves.toEqual({ path: API_PATHS.status });
    await expect(readActiveTasks(config)).resolves.toEqual({ path: API_PATHS.activeTasks });
    await expect(readScheduledReminders(config)).resolves.toEqual({ path: API_PATHS.scheduledReminders });

    expect(seen).toEqual([
      { method: "GET", url: API_PATHS.status, authorization: `Bearer ${token}` },
      { method: "GET", url: API_PATHS.activeTasks, authorization: `Bearer ${token}` },
      { method: "GET", url: API_PATHS.scheduledReminders, authorization: `Bearer ${token}` },
    ]);
  });

  it("requires HTTPS except for literal loopback hosts", () => {
    expect(validateBaseUrl("https://assistant.example").origin).toBe("https://assistant.example");
    expect(validateBaseUrl("http://localhost:8080").origin).toBe("http://localhost:8080");
    expect(validateBaseUrl("http://127.9.8.7:8080").origin).toBe("http://127.9.8.7:8080");
    expect(validateBaseUrl("http://[::1]:8080").origin).toBe("http://[::1]:8080");
    expect(() => validateBaseUrl("http://assistant.example")).toThrow(/HTTPS/);
    expect(() => validateBaseUrl("http://192.168.1.2")).toThrow(/HTTPS/);
  });

  it("allows only an HTTP loopback origin for the optional proxy", () => {
    expect(validateProxyUrl("http://127.0.0.1:11056").origin).toBe("http://127.0.0.1:11056");
    expect(() => validateProxyUrl("https://127.0.0.1:11056")).toThrow(/HTTP loopback/);
    expect(() => validateProxyUrl("http://proxy.example:11056")).toThrow(/HTTPS|HTTP loopback/);
    expect(() => validateProxyUrl("http://127.0.0.1:11056/path")).toThrow(/only an origin/);
  });

  it("rejects credentials and all base URL path, query, and fragment components", () => {
    expect(() => validateBaseUrl("https://user:pass@assistant.example")).toThrow(/credentials/);
    expect(() => validateBaseUrl("https://assistant.example/prefix")).toThrow(/only an origin/);
    expect(() => validateBaseUrl("https://assistant.example?next=evil")).toThrow(/only an origin/);
    expect(() => validateBaseUrl("https://assistant.example#fragment")).toThrow(/only an origin/);
  });

  it("enforces the response byte limit", async () => {
    const oversizedFetch = (async () => new Response(JSON.stringify({ value: "x".repeat(200) }))) as typeof fetch;
    await expect(
      requestJson({ baseUrl: "https://assistant.example", token }, API_PATHS.status, {
        fetchImpl: oversizedFetch,
        maxResponseBytes: 64,
      }),
    ).rejects.toThrow(/64-byte limit/);
  });

  it("enforces the request timeout and does not expose the token in errors", async () => {
    const hangingFetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      await new Promise<never>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      });
      throw new Error("unreachable");
    }) as typeof fetch;

    let error: Error | undefined;
    try {
      await requestJson({ baseUrl: "https://assistant.example", token }, API_PATHS.status, {
        fetchImpl: hangingFetch,
        timeoutMs: 20,
      });
    } catch (caught) {
      error = caught as Error;
    }
    expect(error?.message).toMatch(/timed out after 20 ms/);
    expect(error?.message).not.toContain(token);
  });

  it("returns status-only HTTP errors without echoing response bodies or tokens", async () => {
    const deniedFetch = (async (_request: RequestInfo | URL, _init?: RequestInit) =>
      new Response(`denied ${token}`, { status: 403 })) as typeof fetch;
    await expect(
      requestJson({ baseUrl: "https://assistant.example", token }, API_PATHS.status, { fetchImpl: deniedFetch }),
    ).rejects.toThrow("Personal Assistant API returned HTTP 403.");
  });
});
