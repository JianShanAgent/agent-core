// 后端用的代号 → 给人看的说法。

import type { TraceRow } from "./types";

export const ROUTE_LABELS: Record<string, string> = {
  research: "查证",
  chat: "闲聊",
  command: "命令",
  keyword: "关键词",
  session: "会话中",
  limited: "被限流",
  dropped: "被丢弃",
  none: "没处理",
};

export const OUTCOME_LABELS: Record<string, string> = {
  answered: "查到了",
  not_found: "没查到",
  failed: "查证失败",
};

export const STATUS_LABELS: Record<string, string> = {
  replied: "已回复",
  silent: "没回复",
  error: "出错",
};

export const CHANNEL_LABELS: Record<string, string> = { qq: "QQ", web: "网页对话" };
export const SCOPE_LABELS: Record<string, string> = {
  c2c: "单聊",
  group: "群聊",
  guild: "频道",
  dm: "频道私信",
};

/** 处理过程里每一步的种类，以及模型调用是哪个环节发起的。 */
export const STEP_LABELS: Record<string, string> = {
  entry: "对话入口",
  analyze: "分析问题",
  image: "以图搜图",
  kb: "查知识库",
  site: "站内搜索",
  web: "联网搜索",
  analyst: "查证员",
  review: "复核员",
  revise: "打回重写",
  fix: "快速修正",
  fill: "补整页",
  tool: "工具",
  command: "命令",
  keyword: "关键词",
  progress: "进度消息",
  test: "测试模型",
  "": "其它",
};

export const KB_TYPE_LABELS: Record<string, string> = {
  entry: "条目",
  announcement: "公告",
  story: "剧情",
  data: "数据",
};

export const MODE_LABELS: Record<string, string> = {
  hybrid: "向量 + 关键词",
  vector: "只有向量",
  keyword: "只有关键词",
  none: "什么都没查到",
};

export const label = (table: Record<string, string>, key: string | null | undefined): string =>
  (key !== null && key !== undefined && table[key]) || key || "—";

export type Tone = "default" | "success" | "warning" | "danger" | "info" | "muted";

/** 一条处理记录的结果：一句话 + 用什么颜色。 */
export function traceResult(row: Pick<TraceRow, "status" | "outcome" | "route" | "finished">): {
  text: string;
  tone: Tone;
} {
  if (row.finished === false) return { text: "处理中", tone: "info" };
  if (row.status === "error") return { text: "出错", tone: "danger" };
  if (row.outcome === "failed") return { text: "查证失败", tone: "danger" };
  if (row.outcome === "not_found") return { text: "没查到", tone: "warning" };
  if (row.outcome === "answered") return { text: "查到了", tone: "success" };
  if (row.status === "silent") return { text: "没回复", tone: "muted" };
  if (row.route === "limited") return { text: "被限流", tone: "warning" };
  return { text: "已回复", tone: "success" };
}

/** 谁发的：备注名 > 昵称 > 标识的开头几位。 */
export function who(row: {
  user_label?: string;
  user_name?: string | null;
  user_id?: string;
}): string {
  return row.user_label || row.user_name || (row.user_id ? row.user_id.slice(0, 8) : "—");
}

export const RANGES: { hours: number; label: string }[] = [
  { hours: 1, label: "1 小时" },
  { hours: 24, label: "24 小时" },
  { hours: 24 * 7, label: "7 天" },
  { hours: 24 * 30, label: "30 天" },
];
