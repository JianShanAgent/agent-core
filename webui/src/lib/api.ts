// 调后端接口。登录靠 Cookie（浏览器自己带）；改东西的请求要带一个自定义请求头（见 admin/auth.py）。

import type {
  CatalogInfo,
  ChatLogMessage,
  ChatSummary,
  ConfigFile,
  ConfigFileSummary,
  ConfigState,
  ConfigValues,
  ImageSearchHit,
  KbChunk,
  KbChunkRow,
  KbImage,
  KbSearchResult,
  KbSummary,
  Labels,
  LlmCall,
  LlmCallDetail,
  LogEntry,
  ModelsInfo,
  ModelTestResult,
  Overview,
  Paged,
  PluginsInfo,
  SaveResult,
  SessionDetail,
  SessionRow,
  Status,
  SystemInfo,
  TraceDetail,
  TraceRow,
  UpdateInfo,
  WikiInfo,
  WikiKbState,
  WikisInfo,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** 登录过期时通知应用回到登录页。 */
let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

type Query = Record<string, string | number | boolean | null | undefined>;

function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "" || value === false) continue;
    params.set(key, value === true ? "1" : String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-Requested-With"] = "jianshanagent-admin";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(withQuery(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "连不上机器人（它可能正在重启，或者已经停了）");
  }
  let data: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    const message =
      (data as { error?: string } | null)?.error ?? `请求失败（HTTP ${response.status}）`;
    if (response.status === 401 && path !== "/api/login") onUnauthorized();
    throw new ApiError(response.status, message);
  }
  return data as T;
}

const get = <T>(path: string, query?: Query) => request<T>("GET", path, undefined, query);
const post = <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {});
const put = <T>(path: string, body: unknown) => request<T>("PUT", path, body);
const del = <T>(path: string) => request<T>("DELETE", path);
const seg = encodeURIComponent;

export interface TraceFilters {
  channel?: string;
  route?: string;
  status?: string;
  outcome?: string;
  session?: string;
  chat?: string;
  q?: string;
  min_seconds?: number;
  problems?: boolean;
  before?: string;
  limit?: number;
}

export const api = {
  session: () => get<{ authenticated: boolean; locked_seconds: number }>("/api/session"),
  login: (token: string) => post<{ authenticated: boolean }>("/api/login", { token }),
  logout: () => post<{ authenticated: boolean }>("/api/logout"),

  overview: (hours: number, channel?: string) => get<Overview>("/api/overview", { hours, channel }),
  status: () => get<Status>("/api/status"),
  system: () => get<SystemInfo>("/api/system"),
  reload: () => post<{ status: Status }>("/api/system/reload"),
  restart: () => post<{ restarting: boolean }>("/api/system/restart"),
  update: () => get<UpdateInfo>("/api/update"),
  checkUpdate: () => post<UpdateInfo>("/api/update/check"),
  applyUpdate: () => post<{ restarting: boolean; version: string }>("/api/update/apply"),
  revertUpdate: () => post<{ restarting: boolean; version: string }>("/api/update/revert"),
  logs: (after: number, limit = 1000) =>
    get<{ items: LogEntry[]; last: number }>("/api/logs", { after, limit }),

  traces: (filters: TraceFilters) =>
    get<{ items: TraceRow[]; active: TraceRow[]; more: boolean; stored: boolean }>(
      "/api/traces",
      filters as Query,
    ),
  trace: (id: string) => get<TraceDetail>(`/api/traces/${seg(id)}`),
  calls: (query: {
    profile?: string;
    purpose?: string;
    failed?: boolean;
    before?: number;
    limit?: number;
  }) => get<{ items: LlmCall[]; more: boolean }>("/api/llm-calls", query),
  call: (id: number, traceId?: string | null) =>
    get<LlmCallDetail>(`/api/llm-calls/${id}`, { trace: traceId ?? undefined }),

  configList: () => get<{ files: ConfigFileSummary[]; state: ConfigState }>("/api/config"),
  config: (name: string) => get<ConfigFile>(`/api/config/${name}`),
  saveConfig: (name: string, values: ConfigValues) =>
    put<SaveResult>(`/api/config/${name}`, { values }),
  saveConfigs: (files: Record<string, ConfigValues>) => put<SaveResult>("/api/config", { files }),

  models: () => get<ModelsInfo>("/api/models"),
  testModel: (profile: string) => post<ModelTestResult>("/api/models/test", { profile }),
  plugins: () => get<PluginsInfo>("/api/plugins"),
  savePlugins: (enabled: string[]) => put<SaveResult>("/api/plugins", { enabled }),

  wikis: () => get<WikisInfo>("/api/wikis"),
  wikiCatalog: (refresh = false) => get<CatalogInfo>("/api/wikis/catalog", { refresh }),
  addWiki: (site: string, id: string, label: string) =>
    post<{ added: WikiInfo; downloaded: string | null; status: Status }>("/api/wikis", {
      site,
      id,
      label,
    }),
  removeWiki: (id: string) =>
    del<{ removed: string; site_removed: boolean; status: Status }>(`/api/wikis/${seg(id)}`),
  buildWikiKb: (id: string) =>
    post<{ kb: WikiKbState; sync_alive: boolean }>(`/api/wikis/${seg(id)}/kb`),
  cancelWikiKb: (id: string) => del<{ kb: WikiKbState }>(`/api/wikis/${seg(id)}/kb`),
  updateSitePlugin: (site: string) =>
    post<{ version: string; updated: boolean; restarting: boolean }>(
      `/api/wikis/sites/${seg(site)}/update`,
    ),

  kbSummary: () => get<KbSummary>("/api/kb/summary"),
  kbChunks: (query: {
    q?: string;
    type?: string;
    wiki?: string;
    limit?: number;
    offset?: number;
  }) =>
    get<Paged<KbChunkRow>>("/api/kb/chunks", query),
  kbChunk: (id: string) => get<KbChunk>(`/api/kb/chunks/${seg(id)}`),
  kbSearch: (query: string, topK?: number) =>
    post<KbSearchResult>("/api/kb/search", { query, top_k: topK }),
  kbImages: (query: {
    q?: string;
    kind?: string;
    wiki?: string;
    limit?: number;
    offset?: number;
  }) =>
    get<Paged<KbImage>>("/api/kb/images", query),
  kbImageSearch: (image: string, topK?: number) =>
    post<{ seconds: number; hits: ImageSearchHit[] }>("/api/kb/image-search", {
      image,
      top_k: topK,
    }),

  // days：只列最近这么多天有动静的会话；0 = 不限时间（要把几张表整个过一遍，慢）
  sessions: (query: { q?: string; limit?: number; offset?: number; days?: number }) =>
    get<Paged<SessionRow> & { days: number }>("/api/sessions", query),
  sessionDetail: (key: string) => get<SessionDetail>(`/api/sessions/${seg(key)}`),
  clearHistory: (key: string) => del<{ deleted: number }>(`/api/sessions/${seg(key)}/history`),
  clearMemories: (key: string) => del<{ deleted: number }>(`/api/sessions/${seg(key)}/memories`),
  deleteMemory: (id: number) => del<{ deleted: number }>(`/api/memories/${id}`),

  // days：只列最近这么多天有消息的群，条数、人数也只算这几天的；0 = 不限时间
  chats: (days?: number) =>
    get<{ items: ChatSummary[]; days: number }>("/api/chatlog/chats", { days }),
  chatMessages: (
    chatId: string,
    query: { q?: string; before?: number; limit?: number; addressed?: boolean; user?: string },
  ) =>
    get<{ items: ChatLogMessage[]; more: boolean }>(
      `/api/chatlog/chats/${seg(chatId)}/messages`,
      query,
    ),
  labels: () => get<Labels>("/api/labels"),
  setLabel: (kind: "chat" | "user", id: string, label: string) =>
    put<{ kind: string; id: string; label: string }>("/api/labels", { kind, id, label }),

  resetChat: (user: string, scope: string) =>
    post<{ session: string; deleted: number }>("/api/chat/reset", { user, scope }),
};

// ---- 网页对话：回应是一条事件流 ----

export type ChatEvent =
  | { type: "start"; message_id: string; session: string }
  | { type: "progress"; content: string }
  | {
      type: "reply";
      content: string;
      markdown: boolean;
      buttons: { label: string; command: string }[];
    }
  | { type: "silent" }
  | { type: "error"; message: string }
  | { type: "done"; trace_id: string | null; seconds: number };

export interface ChatRequest {
  text: string;
  images: string[];
  user: string;
  scope: "c2c" | "group";
}

/** 发一条网页对话，事件到一个就交给 `onEvent` 一个；全部结束后返回。 */
export async function sendChat(
  body: ChatRequest,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "jianshanagent-admin" },
      body: JSON.stringify(body),
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError(0, "连不上机器人（它可能正在重启，或者已经停了）");
  }
  if (!response.ok || !response.body) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      message = ((await response.json()) as { error?: string }).error ?? message;
    } catch {
      /* 不是 JSON 就用上面那句 */
    }
    if (response.status === 401) onUnauthorized();
    throw new ApiError(response.status, message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (error) {
      if ((error as Error).name === "AbortError") throw error;
      throw new ApiError(0, "和机器人的连接中途断了（它可能重启了）。这条消息它多半还在处理，去「查证过程」里看结果");
    }
    const { value, done } = chunk;
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let name = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (name) {
        try {
          onEvent({ type: name, ...(JSON.parse(data || "{}") as object) } as ChatEvent);
        } catch {
          /* 坏掉的一块：跳过 */
        }
      }
      cut = buffer.indexOf("\n\n");
    }
  }
}
