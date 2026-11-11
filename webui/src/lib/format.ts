// 数字、时间、用时的显示。

const pad = (value: number) => String(value).padStart(2, "0");

export function parseTime(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 11-11 17:45:01；今天的只显示时间。 */
export function formatTime(
  value: string | number | null | undefined,
  options: { seconds?: boolean; alwaysDate?: boolean } = {},
): string {
  const date = parseTime(value);
  if (!date) return "—";
  const now = new Date();
  const time =
    `${pad(date.getHours())}:${pad(date.getMinutes())}` +
    (options.seconds === false ? "" : `:${pad(date.getSeconds())}`);
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay && !options.alwaysDate) return time;
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return date.getFullYear() === now.getFullYear()
    ? `${day} ${time}`
    : `${date.getFullYear()}-${day} ${time}`;
}

export function formatDateTime(value: string | number | null | undefined): string {
  const date = parseTime(value);
  if (!date) return "—";
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** 3 分钟前、2 小时前、昨天…… */
export function formatAgo(value: string | number | null | undefined): string {
  const date = parseTime(value);
  if (!date) return "—";
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 10) return "刚刚";
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`;
  return formatTime(value, { seconds: false, alwaysDate: true });
}

/** 用时：0.21 秒、13.2 秒、1 分 12 秒。 */
export function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value < 0.995) return `${value.toFixed(2)} 秒`;
  if (value < 60) return `${value.toFixed(1)} 秒`;
  const minutes = Math.floor(value / 60);
  const rest = Math.round(value - minutes * 60);
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

/** 运行了多久：3 天 4 小时、5 小时 12 分、8 分钟。 */
export function formatUptime(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时 ${minutes % 60} 分`;
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
}

/** 12,345 */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Math.round(value).toLocaleString("en-US");
}

/** token 数：9,228 → 9.2k，1,234,567 → 1.23M。 */
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value < 1000) return String(Math.round(value));
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(2)}M`;
}

export function formatPercent(part: number, whole: number): string {
  if (!whole) return "—";
  const value = (part / whole) * 100;
  return `${value >= 99.95 || value < 0.05 ? value.toFixed(0) : value.toFixed(1)}%`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

/** 平台给的标识是一长串：只显示开头几位。 */
export function shortId(id: string, length = 8): string {
  return id.length > length + 1 ? `${id.slice(0, length)}…` : id;
}

/** 图表横轴：按小时是「17 时」，按天是「11-11」。 */
export function formatBucket(value: string, daily: boolean): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return daily ? `${pad(date.getMonth() + 1)}-${pad(date.getDate())}` : `${pad(date.getHours())}时`;
}
