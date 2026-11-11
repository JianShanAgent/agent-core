// 网页对话的状态：屏幕上的记录长什么样，以及「正在进行的那一次请求」。
//
// 请求不跟着页面走：离开这一页时组件没了，请求还在跑（机器人那边也照常处理）。所以它记在这个
// 模块里，不记在组件里——回到这一页能接着看进度；回复到的时候页面不在，就直接改浏览器里存的
// 那份记录，回来就能看到。这样也不会有「往已经卸载的组件上更新状态」的事。

import { useSyncExternalStore } from "react";

import { ApiError, type ChatEvent, type ChatRequest, sendChat } from "@/lib/api";

/** 屏幕上的记录存在浏览器里用的键（页面用 useStored 读写）。 */
export const LOG_KEY = "jianshanagent.chat.log";
/** 只留最近这么多条 */
const KEEP = 200;

/** 没填身份时用的名字（和后端 core.admin 里 web_chat.user 的默认值一样）。 */
export const DEFAULT_USER = "admin";
const USER_CHARS = 32;

/** 身份只留字母、数字、汉字和连字符，最多 32 个字。 */
export function cleanUser(raw: string): string {
  return raw.replace(/[^\p{L}\p{N}-]/gu, "").slice(0, USER_CHARS);
}

export interface ChatButton {
  label: string;
  command: string;
}

export interface UserEntry {
  id: string;
  kind: "user";
  text: string;
  /** 带了几张图。图片内容不存（太大），只在这次打开页面期间留在内存里 */
  images: number;
}

export interface BotEntry {
  id: string;
  kind: "bot";
  /** pending = 还在等回复。存着的 pending 如果不是正在进行的那一次，就是页面刷新前没等到的 */
  state: "pending" | "reply" | "silent" | "error";
  content: string;
  markdown: boolean;
  buttons: ChatButton[];
  /** 处理中途收到过的进度，一句一条（处理完以后也留着） */
  progress: string[];
  seconds: number | null;
  traceId: string | null;
}

/** 屏幕上的一条分隔线（开了新话题）。 */
export interface DividerEntry {
  id: string;
  kind: "divider";
  text: string;
}

export type Entry = UserEntry | BotEntry | DividerEntry;

/** 正在进行的那一次请求。 */
export interface Flight {
  /** 等着回复的那一条的编号 */
  id: string;
  /** 用户发的那一条的编号 */
  askId: string;
  startedAt: number;
  /** 已经收到的进度，一句一条 */
  progress: string[];
}

type Change = (log: Entry[]) => Entry[];

const DROPPED = "连接中断了，没等到回复（机器人可能重启了；处理如果还在进行，可以去查证过程里看）";

let flight: Flight | null = null;
const watchers = new Set<() => void>();
/** 页面开着时，改记录的事交给它 */
let page: ((change: Change) => void) | null = null;
/** 这次打开页面以来发过的图片（按用户那一条的编号），刷新就没了 */
const pictures = new Map<string, string[]>();

function setFlight(next: Flight | null): void {
  flight = next;
  for (const notify of [...watchers]) notify();
}

function watch(notify: () => void): () => void {
  watchers.add(notify);
  return () => watchers.delete(notify);
}

/** 正在进行的请求；没有就是 null。 */
export function useFlight(): Flight | null {
  return useSyncExternalStore(watch, () => flight);
}

export function picturesOf(id: string): string[] {
  return pictures.get(id) ?? [];
}

/** 只留最近的 KEEP 条；不在记录里的图片也一起丢掉。 */
export function tidy(log: Entry[]): Entry[] {
  const kept = log.length > KEEP ? log.slice(log.length - KEEP) : log;
  if (pictures.size) {
    const ids = new Set(kept.map((entry) => entry.id));
    for (const id of [...pictures.keys()]) if (!ids.has(id)) pictures.delete(id);
  }
  return kept;
}

/** 页面挂上来：之后改记录都交给 `apply`。返回的函数在页面卸载时调用。 */
export function attach(apply: (change: Change) => void): () => void {
  page = apply;
  return () => {
    if (page === apply) page = null;
  };
}

/** 浏览器里存的那份记录。 */
export function loadLog(): Entry[] {
  try {
    const log: unknown = JSON.parse(localStorage.getItem(LOG_KEY) ?? "[]");
    return Array.isArray(log) ? (log as Entry[]) : [];
  } catch {
    return [];
  }
}

function change(update: Change): void {
  if (page) {
    page(update);
    return;
  }
  // 页面不在：直接改浏览器里存的那一份
  try {
    localStorage.setItem(LOG_KEY, JSON.stringify(tidy(update(loadLog()))));
  } catch {
    /* 存不下：这一条就不记了 */
  }
}

const newId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** 在屏幕上的记录最后加一条分隔线（页面不在时直接改浏览器里存的那份）。 */
export function addDivider(text: string): void {
  change((log) => {
    const last = log[log.length - 1];
    // 连着加两条一样的没有意义
    if (last?.kind === "divider" && last.text === text) return log;
    return [...log, { id: newId(), kind: "divider", text }];
  });
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 413) {
      return "这条消息太大了（图片转码后，一次请求不能超过 24 MB）。去掉几张图或者换小一点的再发。";
    }
    return error.message;
  }
  // 回复传到一半断了：浏览器给的是一句英文的网络错误，换成能看懂的
  return DROPPED;
}

async function run(id: string, body: ChatRequest): Promise<void> {
  type Outcome = Pick<BotEntry, "state" | "content" | "markdown" | "buttons">;
  const failure = (content: string): Outcome => ({
    state: "error",
    content,
    markdown: false,
    buttons: [],
  });
  const got: { outcome: Outcome | null; seconds: number | null; traceId: string | null } = {
    outcome: null,
    seconds: null,
    traceId: null,
  };
  const onEvent = (event: ChatEvent) => {
    switch (event.type) {
      case "progress":
        if (flight?.id === id) setFlight({ ...flight, progress: [...flight.progress, event.content] });
        break;
      case "reply":
        got.outcome = {
          state: "reply",
          content: event.content,
          markdown: event.markdown,
          buttons: event.buttons ?? [],
        };
        break;
      case "silent":
        got.outcome = { state: "silent", content: "", markdown: false, buttons: [] };
        break;
      case "error":
        got.outcome = failure(event.message);
        break;
      case "done":
        got.seconds = event.seconds;
        got.traceId = event.trace_id;
        break;
      default:
        break;
    }
  };
  try {
    await sendChat(body, onEvent);
  } catch (error) {
    // 回复已经到了、只是最后收尾时断了的，以到了的回复为准
    got.outcome ??= failure(describe(error));
  }
  const settled: BotEntry = {
    id,
    kind: "bot",
    ...(got.outcome ?? failure(DROPPED)),
    // 中途的进度跟着这条回复一起留下来
    progress: flight?.id === id ? flight.progress : [],
    seconds: got.seconds,
    traceId: got.traceId,
  };
  change((log) =>
    log.some((entry) => entry.id === id)
      ? log.map((entry) => (entry.id === id ? settled : entry))
      : [...log, settled],
  );
  setFlight(null);
}

/** 发一条。一次只处理一条：上一条还没回完就不发，返回 false。 */
export function ask(body: ChatRequest): boolean {
  if (flight) return false;
  const askId = newId();
  const id = newId();
  if (body.images.length) pictures.set(askId, body.images);
  change((log) => [
    ...log,
    { id: askId, kind: "user", text: body.text, images: body.images.length },
    {
      id,
      kind: "bot",
      state: "pending",
      content: "",
      markdown: false,
      buttons: [],
      progress: [],
      seconds: null,
      traceId: null,
    },
  ]);
  setFlight({ id, askId, startedAt: Date.now(), progress: [] });
  void run(id, body);
  return true;
}
