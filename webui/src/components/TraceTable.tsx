// 处理记录的列表（首页的「最近的消息」、查证过程页、某个会话的记录都用它）。

import { ChevronRight, Image as ImageIcon } from "lucide-react";
import { useNavigate } from "react-router";

import { ToneBadge } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Hint, Spinner, Table, Td, Th, Tr } from "@/components/ui/misc";
import { formatSeconds, formatTime, formatTokens } from "@/lib/format";
import {
  CHANNEL_LABELS,
  label,
  ROUTE_LABELS,
  SCOPE_LABELS,
  STEP_LABELS,
  traceResult,
  who,
} from "@/lib/labels";
import type { TraceRow } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 这条消息走过的阶段：一串小标签（查证才有）。 */
export function StageChips({ row }: { row: TraceRow }) {
  const research = row.summary?.research;
  const stages = research?.stages ?? [];
  if (!stages.length) return null;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1">
      {stages.map((stage, index) => (
        <span key={`${stage}-${index}`} className="flex items-center gap-1">
          {index > 0 ? <ChevronRight className="text-muted-foreground/60 size-3" /> : null}
          <span
            className={cn(
              "text-muted-foreground rounded bg-muted px-1 py-px text-[11px]",
              (stage === "revise" || stage === "fix") && "bg-warning/15 text-foreground/80",
            )}
          >
            {label(STEP_LABELS, stage)}
          </span>
        </span>
      ))}
    </div>
  );
}

export function TraceResultBadge({ row }: { row: TraceRow }) {
  const result = traceResult(row);
  const rejections = row.summary?.research?.rejections ?? 0;
  return (
    <div className="flex flex-wrap items-center gap-1">
      <ToneBadge tone={result.tone}>
        {row.finished === false ? <Spinner className="size-3 text-current" /> : null}
        {result.text}
      </ToneBadge>
      {rejections > 0 ? (
        <Hint text={`复核打回了 ${rejections} 次${row.summary?.research?.fixed ? "，最后是按复核意见快速修正的" : ""}`}>
          <Badge tone="warning">打回 {rejections}</Badge>
        </Hint>
      ) : null}
    </div>
  );
}

export function TraceTable({
  rows,
  compact = false,
  showWho = true,
}: {
  rows: TraceRow[];
  /** 紧凑：不显示阶段和 token（首页用） */
  compact?: boolean;
  showWho?: boolean;
}) {
  const navigate = useNavigate();
  return (
    <Table>
      <thead>
        <tr>
          <Th className="w-24">时间</Th>
          {showWho ? <Th className="w-36">谁</Th> : null}
          <Th>消息</Th>
          <Th className="w-20">走哪条路</Th>
          <Th className="w-32">结果</Th>
          <Th className="w-20 text-right">用时</Th>
          {compact ? null : <Th className="w-24 text-right">模型</Th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <Tr key={row.id} className="cursor-pointer" onClick={() => navigate(`/traces/${row.id}`)}>
            <Td className="text-muted-foreground text-xs whitespace-nowrap tabular-nums">
              {formatTime(row.started_at)}
            </Td>
            {showWho ? (
              <Td className="max-w-36">
                <div className="truncate text-sm">{who(row)}</div>
                <div className="text-muted-foreground truncate text-[11px]">
                  {label(CHANNEL_LABELS, row.channel)} ·{" "}
                  {row.scope === "group" && row.chat_label
                    ? row.chat_label
                    : label(SCOPE_LABELS, row.scope)}
                </div>
              </Td>
            ) : null}
            <Td className="max-w-0">
              <div className="flex items-center gap-1.5">
                <span className="truncate font-medium">{row.question || "（只发了图片）"}</span>
                {row.images > 0 ? (
                  <Hint text={`带了 ${row.images} 张图片`}>
                    <ImageIcon className="text-muted-foreground size-3.5 shrink-0" />
                  </Hint>
                ) : null}
              </div>
              {row.finished === false ? (
                <div className="text-info mt-0.5 truncate text-xs">
                  {row.stage ? `正在：${row.stage}` : "刚开始处理"}
                </div>
              ) : row.error ? (
                <div className="text-destructive mt-0.5 truncate text-xs">{row.error}</div>
              ) : (
                <div className="text-muted-foreground mt-0.5 truncate text-xs">{row.reply}</div>
              )}
              {compact ? null : <StageChips row={row} />}
            </Td>
            <Td className="whitespace-nowrap">
              <Badge tone={row.route === "research" ? "brand" : "muted"}>
                {row.route === "command" && row.command
                  ? `/${row.command}`
                  : label(ROUTE_LABELS, row.route)}
              </Badge>
            </Td>
            <Td>
              <TraceResultBadge row={row} />
            </Td>
            <Td
              className={cn(
                "text-right whitespace-nowrap tabular-nums",
                row.seconds >= 60 && "text-warning font-medium",
              )}
            >
              {formatSeconds(row.seconds)}
            </Td>
            {compact ? null : (
              <Td className="text-muted-foreground text-right text-xs whitespace-nowrap tabular-nums">
                {row.llm_calls ? (
                  <>
                    {row.llm_calls} 次
                    <div>{formatTokens(row.prompt_tokens + row.completion_tokens)} token</div>
                  </>
                ) : (
                  "—"
                )}
              </Td>
            )}
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}
