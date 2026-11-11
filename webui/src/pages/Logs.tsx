// 实时日志：机器人进程在内存里留着最近 5000 条，这里每隔一秒多取一次新的接在后面。

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Download, Eraser, ExternalLink, Pause, Play, Search } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { CopyButton, EmptyState, LoadingRows, PageHeader } from "@/components/common";
import { FullHeight } from "@/components/layout/AppShell";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Switch } from "@/components/ui/form";
import { Pre, Spinner } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useDebounced, useStored } from "@/lib/hooks";
import type { LogEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 每次最多取多少条（接口的上限，也是机器人内存里留的条数）：刚打开时是全部，暂停很久再继续也不会漏 */
const FETCH_LIMIT = 5000;
/** 页面上最多留多少条，多了丢最旧的 */
const KEEP = 5000;
/** 最多画多少行：再多滚动和筛选都会卡 */
const WINDOW = 1500;
const POLL_MS = 1500;
/** 离上一次取到日志超过这么久（没取到、暂停过、页面在后台被浏览器放慢了），就说不准中间有没有重启过 */
const UNSURE_MS = 4000;
/** 离底部这么近就算在底部 */
const BOTTOM_SLACK = 24;
/** 一行里每个字段的值最多显示多少个字 */
const VALUE_CHARS = 80;
const BRIEF_CHARS = 600;

const RANKS: Record<string, number> = { debug: 10, info: 20, warning: 30, error: 40, critical: 50 };
const LEVEL_OPTIONS = [
  { value: "", label: "级别：全部" },
  { value: "info", label: "info 及以上" },
  { value: "warning", label: "warning 及以上" },
  { value: "error", label: "只看 error" },
];
/** 级别那一格的颜色。warning 往前景色靠一点，浅色主题下小字才看得清（徽标也是这么调的）。 */
const LEVEL_STYLES: Record<string, string> = {
  debug: "text-muted-foreground/70",
  warning: "text-[color-mix(in_oklch,var(--warning),var(--foreground)_30%)]",
  error: "text-destructive",
  critical: "text-destructive",
};
const ROW_TINTS: Record<string, string> = {
  warning: "bg-warning/8",
  error: "bg-destructive/6",
  critical: "bg-destructive/10",
};

/** 页面上的一条日志：显示和筛选要用的东西在收到时就算好，画的时候不再算。 */
interface Line {
  /** 页面上的编号。机器人重启后序号会从头数，所以不能拿序号当 key */
  key: number;
  entry: LogEntry;
  /** 时:分:秒.毫秒 */
  clock: string;
  rank: number;
  /** 跟在事件名后面的 键=值 */
  brief: string;
  /** 有异常堆栈的话，原文 */
  exception: string;
  /** 筛选用：事件名和字段内容，小写 */
  haystack: string;
}

/** 插在日志中间的一行说明（机器人重启了、中间漏了几条）。 */
interface Marker {
  key: number;
  marker: string;
}

type Row = Line | Marker;

const isLine = (row: Row): row is Line => "entry" in row;
const pad = (value: number, length = 2) => String(value).padStart(length, "0");

let serial = 0;

function clockOf(time: string): string {
  const date = new Date(time);
  if (Number.isNaN(date.getTime())) return time;
  return (
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `.${pad(date.getMilliseconds(), 3)}`
  );
}

function briefOf(fields: Record<string, unknown>): string {
  const parts: string[] = [];
  let length = 0;
  for (const [key, value] of Object.entries(fields)) {
    if (key === "exception") continue;
    const isText = typeof value === "string";
    let text = (isText ? value : (JSON.stringify(value) ?? String(value))).replace(/\s+/g, " ");
    const spaced = isText && (text === "" || text.includes(" "));
    if (text.length > VALUE_CHARS) text = `${text.slice(0, VALUE_CHARS)}…`;
    const part = `${key}=${spaced ? `"${text}"` : text}`;
    parts.push(part);
    length += part.length;
    if (length > BRIEF_CHARS) break;
  }
  return parts.join("  ");
}

function toLine(entry: LogEntry): Line {
  const fields = entry.fields ?? {};
  const exception = fields.exception;
  return {
    key: ++serial,
    entry,
    clock: clockOf(entry.time),
    rank: RANKS[entry.level] ?? RANKS.info,
    brief: briefOf(fields),
    exception:
      exception === undefined || exception === null
        ? ""
        : typeof exception === "string"
          ? exception
          : JSON.stringify(exception, null, 2),
    haystack: `${entry.event} ${JSON.stringify(fields)}`.toLowerCase(),
  };
}

const toMarker = (marker: string): Marker => ({ key: ++serial, marker });
const trim = (rows: Row[]): Row[] => (rows.length > KEEP ? rows.slice(rows.length - KEEP) : rows);

/** 点开一行看到的：完整的字段和异常堆栈。 */
function Detail({ line }: { line: Line }) {
  const { entry } = line;
  const { exception: _exception, ...fields } = entry.fields ?? {};
  return (
    <div className="bg-muted/40 border-y px-3 py-2 pl-8">
      <div className="text-muted-foreground mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-sans text-xs">
        <span>{formatDateTime(entry.time)}</span>
        <span>级别 {entry.level}</span>
        <span>序号 {entry.seq}</span>
        <CopyButton text={JSON.stringify(entry)} label="复制这一条（JSON）" />
      </div>
      <div className="mb-1.5 font-semibold break-words whitespace-pre-wrap">{entry.event}</div>
      {Object.keys(fields).length ? (
        <Pre>{JSON.stringify(fields, null, 2)}</Pre>
      ) : line.exception ? null : (
        <div className="text-muted-foreground font-sans">这条日志没有带字段。</div>
      )}
      {line.exception ? <Pre className="text-destructive mt-1.5">{line.exception}</Pre> : null}
    </div>
  );
}

/** 一行日志。每行自己记着展开没展开，来新日志时已经画好的行不用重画。 */
const LogRow = memo(function LogRow({ line, onOpen }: { line: Line; onOpen: () => void }) {
  const [open, setOpen] = useState(false);
  const { entry } = line;
  const toggle = () => {
    // 拖着选字的时候不算点击
    if (window.getSelection()?.toString()) return;
    if (!open) onOpen();
    setOpen(!open);
  };
  return (
    <div className={ROW_TINTS[entry.level]}>
      <div
        className="hover:bg-muted/60 flex cursor-pointer items-center gap-2 px-3 py-px"
        onClick={toggle}
      >
        <ChevronRight
          className={cn(
            "text-muted-foreground/50 size-3 shrink-0 transition-transform",
            open && "rotate-90",
          )}
        />
        <span className="text-muted-foreground shrink-0 tabular-nums">{line.clock}</span>
        <span className={cn("w-16 shrink-0 font-medium", LEVEL_STYLES[entry.level])}>
          {entry.level}
        </span>
        <span className="min-w-0 flex-1 truncate">
          <span className="font-semibold">{entry.event}</span>
          {line.brief ? <span className="text-muted-foreground"> {line.brief}</span> : null}
          {line.exception ? <span className="text-destructive"> （带异常堆栈，点开看）</span> : null}
        </span>
      </div>
      {open ? <Detail line={line} /> : null}
    </div>
  );
});

function MarkerRow({ text }: { text: string }) {
  return (
    <div className="text-muted-foreground bg-muted/50 my-1 border-y py-1 text-center font-sans text-xs">
      —— {text} ——
    </div>
  );
}

export default function Logs() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [offline, setOffline] = useState(false);
  const [paused, setPaused] = useState(false);
  const [follow, setFollow] = useState(true);
  // 不跟的时候，画出来的第一行钉在哪一条上（null = 没钉）。正在往回看时，顶上的行不会因为来了
  // 新日志被换掉，看着的内容就不会自己往上窜；接着跟的时候松开
  const [pinned, setPinned] = useState<number | null>(null);
  const topKey = useRef<number | null>(null);
  const [level, setLevel] = useStored("jianshanagent.logs.level", "");
  const [text, setText] = useState("");
  const needle = useDebounced(text.trim().toLowerCase(), 250);
  const scroller = useRef<HTMLDivElement>(null);
  // 取到哪了：after = 手里最新一条的序号；boot = 那个进程的启动时间；seenAt = 上一次取到是什么时候
  const cursor = useRef({ after: 0, boot: null as number | null, seenAt: 0 });

  const system = useQuery({ queryKey: ["system"], queryFn: api.system, staleTime: 60_000 });
  const logsUrl = system.data?.environment.logs_url;

  useEffect(() => {
    if (paused) return;
    const state = cursor.current;
    let stopped = false;
    let timer = 0;

    // 机器人重启后序号从头数：插一行分隔，下一次从头取
    const restarted = () => {
      const row = toMarker("机器人重启了");
      state.after = 0;
      setRows((previous) => trim([...previous, row]));
    };

    const tick = async () => {
      try {
        // 接口在没有新日志时把 after 原样当 last 返回，重启后序号还没追上来之前看不出重启过。
        // 所以说不准的时候先问一下进程的启动时间：变了就是重启过。
        if (Date.now() - state.seenAt > UNSURE_MS) {
          const status = await api.status();
          if (stopped) return;
          if (state.boot !== null && status.started_at !== state.boot) restarted();
          state.boot = status.started_at;
        }
        const data = await api.logs(state.after, FETCH_LIMIT);
        if (stopped) return;
        if (data.last < state.after) {
          restarted();
        } else {
          const more: Row[] = [];
          const first = data.items[0];
          if (first && state.after > 0 && first.seq > state.after + 1) {
            more.push(
              toMarker(`中间有 ${first.seq - state.after - 1} 条没取到（机器人的内存里已经没有了）`),
            );
          }
          for (const entry of data.items) more.push(toLine(entry));
          if (more.length) setRows((previous) => trim([...previous, ...more]));
          state.after = data.last;
        }
        state.seenAt = Date.now();
        setLoaded(true);
        setOffline(false);
      } catch {
        if (stopped) return;
        state.seenAt = 0;
        setOffline(true);
      }
      timer = window.setTimeout(() => void tick(), POLL_MS);
    };
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [paused]);

  const floor = RANKS[level] ?? 0;
  const { matched, visible, hidden, shown, total } = useMemo(() => {
    let total = 0;
    let shown = 0;
    const matched = rows.filter((row) => {
      if (!isLine(row)) return true;
      total += 1;
      if (row.rank < floor || (needle && !row.haystack.includes(needle))) return false;
      shown += 1;
      return true;
    });
    // 只画最后 WINDOW 行；钉住的时候从钉住的那一行画起
    let start = Math.max(0, matched.length - WINDOW);
    if (pinned !== null) {
      const at = matched.findIndex((row) => row.key >= pinned);
      if (at >= 0 && at < start) start = at;
    }
    const visible = start > 0 ? matched.slice(start) : matched;
    return { matched, visible, hidden: start, shown, total };
  }, [rows, floor, needle, pinned]);

  // 自动滚到底：新日志画出来之后滚到底
  useLayoutEffect(() => {
    const element = scroller.current;
    topKey.current = visible[0]?.key ?? null;
    if (follow && element) element.scrollTop = element.scrollHeight;
  }, [follow, visible]);

  const changeFollow = useCallback((next: boolean) => {
    setFollow(next);
    setPinned((current) => (next ? null : (current ?? topKey.current)));
  }, []);

  // 自己往上滚就不跟了，滚回底部接着跟
  const onScroll = () => {
    const element = scroller.current;
    if (!element) return;
    changeFollow(element.scrollHeight - element.scrollTop - element.clientHeight < BOTTOM_SLACK);
  };

  // 点开一行是要看它：不再往下跟，免得新日志把它顶走
  const stopFollowing = useCallback(() => changeFollow(false), [changeFollow]);

  const exportLogs = () => {
    const lines = matched.filter(isLine).map((row) => JSON.stringify(row.entry));
    const now = new Date();
    const stamp =
      `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
      `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const url = URL.createObjectURL(
      new Blob([`${lines.join("\n")}\n`], { type: "application/x-ndjson" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `jianshanagent-logs-${stamp}.jsonl`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const filtering = Boolean(level || needle);

  return (
    <FullHeight>
      <PageHeader
        title="实时日志"
        description="这里只有机器人这次启动以来最近 5000 条，重启就清空；更早的在服务器的日志系统里（自带的是 Grafana）。"
        actions={
          logsUrl ? (
            <Button asChild variant="outline" size="sm">
              <a href={logsUrl} target="_blank" rel="noreferrer noopener">
                <ExternalLink /> 看更早的日志
              </a>
            </Button>
          ) : null
        }
      />
      <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b p-3">
          <Button
            variant={paused ? "brand" : "outline"}
            size="sm"
            onClick={() => setPaused(!paused)}
          >
            {paused ? <Play /> : <Pause />} {paused ? "继续" : "暂停"}
          </Button>
          <SimpleSelect size="sm" value={level} onChange={setLevel} options={LEVEL_OPTIONS} />
          <div className="relative min-w-44 flex-1">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
            <Input
              className="h-8 pl-8"
              placeholder="筛选：事件名或字段里的字"
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <Switch checked={follow} onCheckedChange={changeFollow} />
            自动滚到底
          </label>
          <Button variant="ghost" size="sm" disabled={rows.length === 0} onClick={() => setRows([])}>
            <Eraser /> 清屏
          </Button>
          <Button variant="ghost" size="sm" disabled={shown === 0} onClick={exportLogs}>
            <Download /> 导出
          </Button>
          <div className="text-muted-foreground ml-auto flex items-center gap-3 text-xs">
            {offline ? (
              <span className="text-destructive flex items-center gap-1.5">
                <Spinner className="size-3 text-current" /> 连不上，重试中…
              </span>
            ) : paused ? (
              <span>已暂停，不取新的</span>
            ) : null}
            <span className="tabular-nums">
              共 {total} 条，显示 {shown} 条
            </span>
          </div>
        </div>
        <div
          ref={scroller}
          onScroll={onScroll}
          className="min-h-0 flex-1 overflow-y-auto py-1 font-mono text-xs leading-5"
        >
          {!loaded ? (
            offline ? (
              <EmptyState
                className="font-sans"
                title="还没取到日志"
                hint="连不上机器人：它可能正在重启，或者已经停了。连上以后这里会自己接着显示。"
              />
            ) : (
              <LoadingRows rows={8} />
            )
          ) : shown === 0 ? (
            <EmptyState
              className="font-sans"
              title={total ? "没有符合筛选条件的日志" : "还没有日志"}
              hint={
                total
                  ? "换个级别或者筛选的字试试；新来的日志符合条件会自己出现。"
                  : filtering
                    ? "新来的日志符合筛选条件会自己出现。"
                    : "新日志来了会自己出现。"
              }
            />
          ) : (
            <>
              {hidden > 0 ? (
                <div className="text-muted-foreground px-3 pb-1 text-center font-sans text-xs">
                  更早的 {hidden} 条没有显示，用筛选或导出
                </div>
              ) : null}
              {visible.map((row) =>
                isLine(row) ? (
                  <LogRow key={row.key} line={row} onOpen={stopFollowing} />
                ) : (
                  <MarkerRow key={row.key} text={row.marker} />
                ),
              )}
            </>
          )}
        </div>
      </Card>
    </FullHeight>
  );
}
