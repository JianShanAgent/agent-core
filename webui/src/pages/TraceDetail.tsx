// 一条消息是怎么处理的：时间线上每一步的结果、用时、模型调用，最后回了什么。

import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bot,
  BookOpen,
  ChevronDown,
  CircleCheck,
  CircleX,
  ExternalLink,
  Globe,
  Images,
  Layers,
  MessageSquareText,
  PenLine,
  Search,
  ShieldCheck,
  Sparkles,
  SquareTerminal,
  Undo2,
  Wrench,
} from "lucide-react";
import { type ComponentType, type ReactNode, useState } from "react";
import { Link, useParams } from "react-router";

import { CallMeta, CallSheet } from "@/components/CallSheet";
import { ChunkDialog } from "@/components/ChunkDialog";
import { CopyButton, ErrorState, Field, LoadingRows, Markdown } from "@/components/common";
import { TraceResultBadge } from "@/components/TraceTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Hint, Pre, Spinner } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber, formatSeconds, formatTokens } from "@/lib/format";
import {
  CHANNEL_LABELS,
  label,
  ROUTE_LABELS,
  SCOPE_LABELS,
  STEP_LABELS,
  who,
} from "@/lib/labels";
import type { EvidenceItem, LlmCall, TraceDetail as Trace, TraceStep } from "@/lib/types";
import { cn } from "@/lib/utils";

const STEP_ICONS: Record<string, ComponentType<{ className?: string }>> = {
  entry: Bot,
  analyze: Sparkles,
  image: Images,
  kb: BookOpen,
  site: Search,
  web: Globe,
  analyst: PenLine,
  review: ShieldCheck,
  fix: Undo2,
  fill: Layers,
  tool: Wrench,
  command: SquareTerminal,
  keyword: SquareTerminal,
  progress: MessageSquareText,
};

const NONE = new Set<number>();
const text = (value: unknown): string => (typeof value === "string" ? value : "");
const list = <T,>(value: unknown): T[] => (Array.isArray(value) ? (value as T[]) : []);

interface NameNote {
  written: string;
  title: string;
  kind: string;
  exact: boolean;
}

/** 每一步在时间线上的一句话概括。 */
function summarize(step: TraceStep): ReactNode {
  const data = step.data;
  switch (step.kind) {
    case "entry": {
      const calls = list<string>(data.calls);
      if (calls.includes("research")) return "判断要查资料，交给查证工作流";
      if (calls.length) return `调用工具：${calls.join("、")}`;
      return text(data.reply) ? `直接回复：${text(data.reply)}` : "直接回复";
    }
    case "analyze": {
      const names = list<NameNote>(data.names);
      const parts = [];
      if (text(data.topic)) parts.push(`归类为「${text(data.topic)}」`);
      if (names.length) {
        parts.push(
          names
            .map((note) =>
              note.written === note.title
                ? `${note.written}（${note.kind}）`
                : `${note.written} ${note.exact ? "→" : "?"} ${note.title}（${note.kind}）`,
            )
            .join("，"),
        );
      }
      if (text(data.time)) parts.push(`时间指 ${text(data.time)}`);
      return parts.length ? parts.join(" · ") : "没摘出名字和时间，按原话查";
    }
    case "image": {
      const identified = list<string>(data.identified);
      return identified.length ? `认出来了：${identified.join("；")}` : "没有认准是哪张图";
    }
    case "kb":
    case "site":
    case "web": {
      const evidence = list<EvidenceItem>(data.evidence);
      const keywords = text(data.keywords);
      return (
        `${keywords ? `搜「${keywords}」，` : ""}新拿到 ${evidence.length} 条资料` +
        (data.from_entries ? `（其中 ${String(data.from_entries)} 条来自点到名的条目自己的页面）` : "") +
        (text(data.error) ? "；这一级出错了" : "")
      );
    }
    case "analyst": {
      if (!step.ok) return text(data.error) || "没做成";
      if (text(data.action) === "search") return `资料不够，升级去搜「${text(data.keywords)}」`;
      const sources = list<number>(data.sources);
      return text(data.answer)
        ? `写了结论${sources.length ? `，依据资料 ${sources.join("、")}` : ""}`
        : "资料答不了，没有写出结论";
    }
    case "review": {
      if (!step.ok) return text(data.error) || "没做成";
      const problems = list<string>(data.problems);
      return data.passed ? "通过" : `不通过，指出 ${problems.length} 个问题`;
    }
    case "fix":
      return text(data.answer) ? "按复核意见改好了，直接发" : "没改出来";
    case "fill": {
      if (!step.ok) return text(data.error) || "没补成";
      const added = Number(data.added ?? 0);
      const excerpts = Number(data.excerpts ?? 0);
      return (
        `同一个页面挑中了好几段的，把整页补给模型：资料多了 ${added} 条、${Number(data.chars ?? 0)} 字` +
        (excerpts ? `（有 ${excerpts} 条页面太长，只给了节选）` : "")
      );
    }
    case "tool":
      return text(data.result);
    case "command":
      return text(data.args) ? `参数：${text(data.args)}` : "";
    default:
      return "";
  }
}

function EvidenceList({
  items,
  cited,
  numbered,
  onOpen,
}: {
  items: EvidenceItem[];
  /** 回答依据的资料编号（只在带编号的清单里标） */
  cited: Set<number>;
  /** 带不带编号。编号是模型看到的【资料 N】，只有查证员那一步的清单是准的 */
  numbered: boolean;
  onOpen: (key: string) => void;
}) {
  if (!items.length) return <div className="text-muted-foreground text-xs">这一级没有拿到新资料。</div>;
  return (
    <div className="flex flex-col divide-y rounded-md border">
      {items.map((item) => {
        // 知识库切片的 key 是切片编号；以图搜图、网页的 key 带前缀或者就是网址
        const isChunk = !item.key.includes(":") && !item.key.startsWith("http");
        const used = numbered && cited.has(item.number);
        return (
          <div key={`${item.number}-${item.key}`} className="px-3 py-2 text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              {numbered ? (
                <span
                  className={cn(
                    "rounded px-1 font-mono tabular-nums",
                    used ? "bg-brand/15 text-brand font-semibold" : "bg-muted",
                  )}
                >
                  {item.number}
                </span>
              ) : null}
              <span className="text-foreground text-[13px] font-medium">{item.title}</span>
              <span className="text-muted-foreground">
                {[
                  item.origin,
                  item.kind,
                  item.date && `日期 ${item.date}`,
                  `${item.chars} 字`,
                  item.excerpt && "节选",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
              {used ? <Badge tone="brand">回答用到了</Badge> : null}
              <span className="ml-auto flex items-center gap-1">
                {isChunk ? (
                  <Button variant="ghost" size="xs" onClick={() => onOpen(item.key)}>
                    看全文
                  </Button>
                ) : null}
                {item.url ? (
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-muted-foreground hover:text-foreground p-1"
                  >
                    <ExternalLink className="size-3.5" />
                  </a>
                ) : null}
              </span>
            </div>
            <div className="text-muted-foreground mt-1 line-clamp-2 leading-relaxed">
              {item.preview}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Labeled({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-muted-foreground mb-1 text-xs">{name}</div>
      {children}
    </div>
  );
}

/** 每一步展开后的详情。 */
function StepDetail({
  step,
  onOpenChunk,
}: {
  step: TraceStep;
  onOpenChunk: (key: string) => void;
}) {
  const data = step.data;
  const blocks: ReactNode[] = [];
  if (text(data.error)) {
    blocks.push(
      <div key="error" className="text-destructive bg-destructive/8 rounded-md px-2.5 py-1.5 text-xs">
        {text(data.error)}
      </div>,
    );
  }
  if (step.kind === "analyze") {
    const names = list<NameNote>(data.names);
    blocks.push(
      <div key="analyze" className="flex flex-col gap-1.5">
        <Field label="类别">{text(data.topic) || "没归上"}</Field>
        <Field label="摘出的名字">
          {names.length ? (
            <div className="flex flex-col gap-0.5">
              {names.map((note, index) => (
                <span key={index}>
                  「{note.written}」
                  {note.exact ? " 是 " : " 没登记，名字里带它的有 "}
                  {note.kind}「{note.title}」
                  {note.exact ? null : <Badge tone="warning" className="ml-1.5">只是候选</Badge>}
                </span>
              ))}
            </div>
          ) : (
            "没有"
          )}
        </Field>
        <Field label="时间">{text(data.time) || "问题里没有相对时间"}</Field>
        <Field label="检索用的问句">{text(data.query)}</Field>
      </div>,
    );
  }
  if (["kb", "site", "web", "image"].includes(step.kind)) {
    if (text(data.query)) {
      blocks.push(
        <Field key="query" label="检索用的问句">
          {text(data.query)}
        </Field>,
      );
    }
    blocks.push(
      <EvidenceList
        key="evidence"
        items={list<EvidenceItem>(data.evidence)}
        cited={NONE}
        numbered={false}
        onOpen={onOpenChunk}
      />,
    );
  }
  if (step.kind === "analyst") {
    const seen = list<EvidenceItem>(data.evidence);
    if (seen.length) {
      blocks.push(
        <details key="seen" className="group">
          <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs select-none">
            这一次给模型看的资料（{seen.length} 条，编号就是它引用时用的编号）
          </summary>
          <div className="mt-2">
            <EvidenceList
              items={seen}
              cited={new Set(list<number>(data.sources))}
              numbered
              onOpen={onOpenChunk}
            />
          </div>
        </details>,
      );
    }
  }
  if (step.kind === "analyst" || step.kind === "fix") {
    if (text(data.answer)) {
      blocks.push(
        <Labeled key="answer" name={step.kind === "fix" ? "改好的回答" : "写的结论"}>
          <div className="bg-muted/50 rounded-md border px-3 py-2">
            <Markdown>{text(data.answer)}</Markdown>
          </div>
        </Labeled>,
      );
    }
    if (text(data.keywords)) {
      blocks.push(
        <Field key="keywords" label="下一级的搜索词">
          {text(data.keywords)}
        </Field>,
      );
    }
  }
  if (step.kind === "review" || step.kind === "fix") {
    const problems = list<string>(data.problems);
    if (text(data.answer) && step.kind === "review") {
      blocks.push(
        <Labeled key="reviewed" name="被复核的结论">
          <div className="bg-muted/50 rounded-md border px-3 py-2">
            <Markdown>{text(data.answer)}</Markdown>
          </div>
        </Labeled>,
      );
    }
    if (problems.length) {
      blocks.push(
        <Labeled key="problems" name="复核指出的问题">
          <ol className="border-warning/40 bg-warning/8 list-decimal rounded-md border py-2 pr-3 pl-7 text-sm leading-relaxed">
            {problems.map((problem, index) => (
              <li key={index}>{problem}</li>
            ))}
          </ol>
        </Labeled>,
      );
    }
  }
  if (step.kind === "tool") {
    blocks.push(
      <Labeled key="args" name="参数">
        <Pre>{JSON.stringify(data.arguments ?? {}, null, 2)}</Pre>
      </Labeled>,
      <Labeled key="result" name={`结果（共 ${formatNumber(Number(data.chars ?? 0))} 字，这里是开头）`}>
        <Pre>{text(data.result)}</Pre>
      </Labeled>,
    );
  }
  if (step.kind === "entry" && text(data.reply)) {
    blocks.push(
      <Labeled key="reply" name="模型的回复">
        <Pre>{text(data.reply)}</Pre>
      </Labeled>,
    );
  }
  if (step.kind === "command" || step.kind === "keyword") {
    blocks.push(
      <Field key="plugin" label="哪个插件">
        {text(data.plugin)}
      </Field>,
    );
  }
  return blocks.length ? <div className="flex flex-col gap-3">{blocks}</div> : null;
}

function StepRow({
  step,
  calls,
  total,
  running,
  onOpenCall,
  onOpenChunk,
}: {
  step: TraceStep;
  calls: LlmCall[];
  total: number;
  running: boolean;
  onOpenCall: (call: LlmCall) => void;
  onOpenChunk: (key: string) => void;
}) {
  const instant = step.seconds === null;
  const [open, setOpen] = useState(
    // 默认展开最值得看的：出错的、复核没通过的
    !step.ok || (step.kind === "review" && step.data.passed === false),
  );
  const Icon = STEP_ICONS[step.kind] ?? Wrench;
  const detail = instant ? null : (
    <StepDetail step={step} onOpenChunk={onOpenChunk} />
  );
  const expandable = detail !== null;
  const left = total > 0 ? Math.min(100, (step.at / total) * 100) : 0;
  const width = total > 0 && step.seconds ? Math.max(1.2, (step.seconds / total) * 100) : 0;
  const failed = !step.ok || (step.kind === "review" && step.data.passed === false);

  if (instant) {
    return (
      <div className="text-muted-foreground flex items-center gap-3 py-1.5 pl-[3px] text-xs">
        <span className="bg-border ml-[9px] size-1.5 shrink-0 rounded-full" />
        <span className="w-14 shrink-0 tabular-nums">+{step.at.toFixed(1)} 秒</span>
        <span>
          {step.kind === "progress" ? "发了一句进度：" : `${label(STEP_LABELS, step.kind)}：`}
          <span className="text-foreground/80">{step.title}</span>
        </span>
      </div>
    );
  }
  return (
    <div className="relative">
      <div
        className={cn(
          "flex items-start gap-3 rounded-lg px-1 py-2",
          expandable && "hover:bg-muted/50 cursor-pointer",
        )}
        onClick={() => expandable && setOpen(!open)}
      >
        <div
          className={cn(
            "bg-card z-10 flex size-7 shrink-0 items-center justify-center rounded-full border",
            failed && "border-destructive/50 text-destructive",
            running && "border-info text-info",
          )}
        >
          {running ? <Spinner className="size-3.5 text-current" /> : <Icon className="size-3.5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-sm font-medium">
              {step.title || label(STEP_LABELS, step.kind)}
            </span>
            {step.kind === "review" && step.ok ? (
              step.data.passed ? (
                <Badge tone="success">
                  <CircleCheck /> 通过
                </Badge>
              ) : (
                <Badge tone="danger">
                  <CircleX /> 打回
                </Badge>
              )
            ) : null}
            {!step.ok ? <Badge tone="danger">没做成</Badge> : null}
            <span className="text-muted-foreground ml-auto flex items-center gap-2 text-xs tabular-nums">
              +{step.at.toFixed(1)} 秒
              <span
                className={cn(
                  "text-foreground font-medium",
                  (step.seconds ?? 0) >= 30 && "text-warning",
                )}
              >
                {running ? "进行中" : formatSeconds(step.seconds)}
              </span>
              {expandable ? (
                <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
              ) : (
                <span className="w-4" />
              )}
            </span>
          </div>
          <div className="text-muted-foreground mt-0.5 line-clamp-2 text-xs leading-relaxed">
            {summarize(step)}
          </div>
          {/* 这一步在整个处理过程里占的位置和长度 */}
          <div className="bg-muted mt-1.5 h-1 overflow-hidden rounded-full">
            <div
              className={cn("h-full rounded-full", failed ? "bg-destructive/70" : "bg-brand/70")}
              style={{ marginLeft: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
            />
          </div>
        </div>
      </div>
      {calls.length ? (
        <div className="mt-0.5 mb-1 ml-11 flex flex-col gap-1">
          {calls.map((call) => (
            <button
              key={call.id}
              type="button"
              onClick={() => onOpenCall(call)}
              className={cn(
                "hover:border-foreground/30 bg-card flex w-full cursor-pointer flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 text-left transition-colors",
                !call.ok && "border-destructive/40",
              )}
            >
              <CallMeta call={call} />
              {!call.ok ? (
                <span className="text-destructive truncate text-xs">{call.error}</span>
              ) : null}
              <span className="text-muted-foreground ml-auto text-xs">
                {call.has_detail ? "看提示词和回复 ›" : ""}
              </span>
            </button>
          ))}
        </div>
      ) : null}
      {open && detail ? <div className="mt-1 mb-3 ml-11">{detail}</div> : null}
    </div>
  );
}

function Summary({ trace }: { trace: Trace }) {
  const research = trace.summary.research;
  return (
    <Card>
      <CardHeader>
        <CardTitle>概况</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Field label="时间">{formatDateTime(trace.started_at)}</Field>
        <Field label="谁">
          {who(trace)}
          <span className="text-muted-foreground">
            {" "}
            · {label(CHANNEL_LABELS, trace.channel)} · {label(SCOPE_LABELS, trace.scope)}
          </span>
        </Field>
        <Field label="会话">
          <Link
            to={`/sessions/${encodeURIComponent(trace.session_key)}`}
            className="text-brand font-mono text-xs break-all hover:underline"
          >
            {trace.session_key}
          </Link>
        </Field>
        <Field label="走哪条路">
          {trace.route === "command" && trace.command
            ? `命令 /${trace.command}`
            : label(ROUTE_LABELS, trace.route)}
        </Field>
        <Field label="一共用时">
          <span className="font-medium">{formatSeconds(trace.seconds)}</span>
        </Field>
        <Field label="模型调用">
          {trace.llm_calls ? (
            <>
              {trace.llm_calls} 次 · 输入 {formatTokens(trace.prompt_tokens)} · 输出{" "}
              {formatTokens(trace.completion_tokens)}
              {trace.reasoning_tokens ? `（思考 ${formatTokens(trace.reasoning_tokens)}）` : ""}
            </>
          ) : (
            "没有调用模型"
          )}
        </Field>
        {research ? (
          <>
            {research.topic ? <Field label="问题类别">{research.topic}</Field> : null}
            <Field label="资料">一共看了 {research.evidence ?? 0} 条</Field>
            {research.rejections ? (
              <Field label="复核">
                打回了 {research.rejections} 次
                {research.fixed ? "，最后按复核意见快速修正后直接发的（没有再复核）" : ""}
              </Field>
            ) : null}
            {research.problems?.length ? (
              <Field label="没解决的问题">
                <ul className="list-disc pl-4">
                  {research.problems.map((problem, index) => (
                    <li key={index}>{problem}</li>
                  ))}
                </ul>
              </Field>
            ) : null}
          </>
        ) : null}
        <Field label="编号">
          <span className="inline-flex items-center gap-1">
            <code className="font-mono text-xs">{trace.id}</code>
            <CopyButton text={trace.id} />
          </span>
        </Field>
      </CardContent>
    </Card>
  );
}

export default function TraceDetail() {
  const { id = "" } = useParams();
  const [call, setCall] = useState<LlmCall | null>(null);
  const [chunk, setChunk] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["trace", id],
    queryFn: () => api.trace(id),
    // 还在处理的：一秒多刷一次，看着它往下走
    refetchInterval: (state) => (state.state.data?.finished === false ? 1_500 : false),
  });
  const trace = query.data;

  if (query.isPending) return <LoadingRows rows={8} />;
  if (query.isError || !trace) {
    return (
      <Card>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    );
  }

  const total = Math.max(trace.seconds, 0.001);
  const callsOf = (stepId: number | null) => trace.calls.filter((item) => item.step_id === stepId);
  const loose = callsOf(null);
  const lastTimed = [...trace.steps].reverse().find((step) => step.seconds !== null);

  return (
    <div className="flex flex-col gap-3">
      <div>
        <Button asChild variant="ghost" size="sm" className="text-muted-foreground -ml-2">
          <Link to="/traces">
            <ArrowLeft /> 查证过程
          </Link>
        </Button>
        <div className="mt-1 flex flex-wrap items-start gap-x-3 gap-y-2">
          <h1 className="min-w-0 flex-1 text-xl font-semibold tracking-tight break-words">
            {trace.question || "（只发了图片）"}
            {trace.images ? (
              <Hint text="这条消息带了图片（图片本身不存）">
                <Badge tone="muted" className="ml-2 align-middle">
                  <Images /> {trace.images} 张图
                </Badge>
              </Hint>
            ) : null}
          </h1>
          <div className="flex items-center gap-2 pt-1">
            <TraceResultBadge row={trace} />
            <span className="text-sm font-medium tabular-nums">{formatSeconds(trace.seconds)}</span>
          </div>
        </div>
      </div>

      <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-3">
          <Card>
            <CardHeader>
              <CardTitle>
                过程
                {trace.finished ? null : (
                  <span className="text-info ml-2 text-xs font-normal">正在处理，自动刷新中…</span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="relative">
              {trace.steps.length ? (
                <>
                  <div className="bg-border absolute top-4 bottom-6 left-[1.6rem] w-px" />
                  {trace.steps.map((step) => (
                    <StepRow
                      key={step.id}
                      step={step}
                      calls={callsOf(step.id)}
                      total={total}
                      running={!trace.finished && step === lastTimed && step.seconds === 0}
                      onOpenCall={setCall}
                      onOpenChunk={setChunk}
                    />
                  ))}
                </>
              ) : (
                <div className="text-muted-foreground py-2 text-sm">
                  {trace.finished
                    ? "这条消息没有分步的过程（直接处理完了）。"
                    : "刚开始处理……"}
                </div>
              )}
              {loose.length ? (
                <div className="mt-2 border-t pt-3">
                  <div className="text-muted-foreground mb-1.5 text-xs">不在任何一步里的模型调用</div>
                  <div className="flex flex-col gap-1">
                    {loose.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setCall(item)}
                        className="hover:border-foreground/30 bg-card flex cursor-pointer flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 text-left"
                      >
                        <CallMeta call={item} />
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}
            </CardContent>
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-3 xl:sticky xl:top-0">
          {trace.error ? (
            <Card className="border-destructive/40">
              <CardHeader>
                <CardTitle className="text-destructive">处理时出错了</CardTitle>
              </CardHeader>
              <CardContent>
                <Pre className="text-destructive">{trace.error}</Pre>
              </CardContent>
            </Card>
          ) : null}
          <Card>
            <CardHeader>
              <CardTitle>{trace.reply ? "发给用户的回复" : "回复"}</CardTitle>
              {trace.reply ? <CopyButton text={trace.reply} /> : null}
            </CardHeader>
            <CardContent>
              {trace.reply ? (
                <Markdown>{trace.reply}</Markdown>
              ) : (
                <div className="text-muted-foreground text-sm">
                  {trace.finished ? "没有回复这条消息。" : "还没处理完。"}
                </div>
              )}
            </CardContent>
          </Card>
          <Summary trace={trace} />
        </div>
      </div>

      <CallSheet call={call} onClose={() => setCall(null)} />
      <ChunkDialog chunkId={chunk} onClose={() => setChunk(null)} />
    </div>
  );
}
