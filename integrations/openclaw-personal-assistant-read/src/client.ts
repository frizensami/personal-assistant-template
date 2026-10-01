export const REQUEST_TIMEOUT_MS = 5_000;
export const MAX_RESPONSE_BYTES = 256 * 1024;

export const API_PATHS = {
  status: "/internal/v1/status",
  activeTasks: "/internal/v1/tasks",
  scheduledReminders: "/internal/v1/reminders",
} as const;

export type ClientConfig = {
  baseUrl: string;
  token: string;
  proxyUrl?: string;
};

type RequestOptions = {
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  maxResponseBytes?: number;
};

function isLoopbackHostname(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (host === "localhost" || host.endsWith(".localhost") || host === "::1") {
    return true;
  }
  const octets = host.split(".");
  return octets.length === 4 && octets[0] === "127" && octets.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
}

export function validateBaseUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Personal Assistant baseUrl must be a valid absolute URL.");
  }

  if (url.username || url.password) {
    throw new Error("Personal Assistant baseUrl must not contain credentials.");
  }
  if (url.search || url.hash || (url.pathname !== "/" && url.pathname !== "")) {
    throw new Error("Personal Assistant baseUrl must contain only an origin, without a path, query, or fragment.");
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && isLoopbackHostname(url.hostname))) {
    throw new Error("Personal Assistant baseUrl must use HTTPS; HTTP is allowed only for loopback hosts.");
  }
  return url;
}

export function validateProxyUrl(value: string): URL {
  const url = validateBaseUrl(value);
  if (url.protocol !== "http:" || !isLoopbackHostname(url.hostname)) {
    throw new Error("Personal Assistant proxyUrl must be an HTTP loopback origin.");
  }
  return url;
}

function endpointUrl(baseUrl: string, path: (typeof API_PATHS)[keyof typeof API_PATHS]): URL {
  const base = validateBaseUrl(baseUrl);
  return new URL(path, base.origin);
}

function byteLengthHeader(response: Response): number | undefined {
  const raw = response.headers.get("content-length");
  if (raw === null || !/^\d+$/.test(raw)) {
    return undefined;
  }
  const value = Number(raw);
  return Number.isSafeInteger(value) ? value : undefined;
}

async function readBoundedBody(response: Response, maxBytes: number): Promise<string> {
  const declaredLength = byteLengthHeader(response);
  if (declaredLength !== undefined && declaredLength > maxBytes) {
    throw new Error(`Personal Assistant response exceeded the ${maxBytes}-byte limit.`);
  }
  if (!response.body) {
    return "";
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > maxBytes) {
        await reader.cancel();
        throw new Error(`Personal Assistant response exceeded the ${maxBytes}-byte limit.`);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const body = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(body);
}

export async function requestJson(
  config: ClientConfig,
  path: (typeof API_PATHS)[keyof typeof API_PATHS],
  options: RequestOptions = {},
): Promise<unknown> {
  if (!config.token.trim()) {
    throw new Error("Personal Assistant token is required.");
  }

  const url = endpointUrl(config.baseUrl, path);
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? REQUEST_TIMEOUT_MS;
  const timeout = setTimeout(() => controller.abort(new Error("request timeout")), timeoutMs);
  const onCallerAbort = () => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", onCallerAbort, { once: true });
  if (options.signal?.aborted) {
    onCallerAbort();
  }

  let response: Response;
  try {
    const requestInit: RequestInit & { dispatcher?: unknown } = {
      method: "GET",
      headers: {
        accept: "application/json",
        authorization: `Bearer ${config.token}`,
      },
      redirect: "error",
      signal: controller.signal,
    };
    if (config.proxyUrl && !options.fetchImpl) {
      const { ProxyAgent } = await import("undici");
      requestInit.dispatcher = new ProxyAgent(validateProxyUrl(config.proxyUrl).toString());
    }
    response = await (options.fetchImpl ?? fetch)(url, requestInit);
  } catch {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", onCallerAbort);
    if (options.signal?.aborted) {
      throw new Error("Personal Assistant request was cancelled.");
    }
    if (controller.signal.aborted) {
      throw new Error(`Personal Assistant request timed out after ${timeoutMs} ms.`);
    }
    throw new Error("Personal Assistant request failed.");
  }

  try {
    if (!response.ok) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error(`Personal Assistant API returned HTTP ${response.status}.`);
    }

    const text = await readBoundedBody(response, options.maxResponseBytes ?? MAX_RESPONSE_BYTES);
    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new Error("Personal Assistant API returned invalid JSON.");
    }
  } catch (error) {
    if (options.signal?.aborted) {
      throw new Error("Personal Assistant request was cancelled.");
    }
    if (controller.signal.aborted) {
      throw new Error(`Personal Assistant request timed out after ${timeoutMs} ms.`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", onCallerAbort);
  }
}

export function readStatus(config: ClientConfig, signal?: AbortSignal): Promise<unknown> {
  return requestJson(config, API_PATHS.status, { signal });
}

export function readActiveTasks(config: ClientConfig, signal?: AbortSignal): Promise<unknown> {
  return requestJson(config, API_PATHS.activeTasks, { signal });
}

export function readScheduledReminders(config: ClientConfig, signal?: AbortSignal): Promise<unknown> {
  return requestJson(config, API_PATHS.scheduledReminders, { signal });
}
