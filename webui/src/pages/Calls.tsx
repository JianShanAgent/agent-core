// 模型调用：每一次调大模型的记录（用量、耗时、成没成）。点一行看提示词和回复。

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Brain, RefreshCw, X } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { CallSheet } from "@/components/CallSheet";
import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/form";
import { Hint, Table, Td, Th, Tr } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import { formatNumber, formatSeconds, formatTime } from "@/lib/format";
import { label, STEP_LABELS } from "@/lib/labels";
import type { LlmCall } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE = 100;
const SLOW_SECONDS = 30;

/** 会调模型的环节（筛选用）。 */
const PURPOSES = ["entry", "analyze", "analyst", "review", "fix", "web", "test"];

/** 正常的结束原因；别的都值得看一眼。 */
const NORMAL_FINISH = ["stop", "tool_calls"];
const FINISH_NOTES: Record<string, { text: string; hint: string }> = {
  length: {
    text: "输出被截断",
    hint: "结束原因 length：输出到了这一档的 max_completion_tokens 上限，后面的没写完。",
  },
  abort: {
    text: "接口中途放弃",
    hint: "结束原因 abort：接口等了一阵后自己放弃了，通常一个字都没返回。",
  },
  content_filter: {
    text: "被内容审核拦下",
    hint: "结束原因 content_filter：输出被接口的内容审核拦下了。",
  },
};

function Result({ call }: { call: LlmCall }) {
  if (!call.ok) {
    return (
      // 限着宽度：报错的话可能很长，不限的话这一列会把别的列都挤窄
      <div className="max-w-64">
        <Badge tone="danger">没成</Badge>
        {call.error ? (
          <div className="text-destructive mt-1 line-clamp-2 text-xs leading-snug break-words">
            {call.error}
          </div>
        ) : null}
      </div>
    );
  }
  if (call.finish_reason && !NORMAL_FINISH.includes(call.finish_reason)) {
    const note = FINISH_NOTES[call.finish_reason];
    return (
      <Hint text={note?.hint ?? `结束原因 ${call.finish_reason}：不是正常写完的。`}>
        <Badge tone="warning">{note?.text ?? `结束原因 ${call.finish_reason}`}</Badge>
      </Hint>
    );
  }
  return <span className="text-muted-foreground text-xs">正常</span>;
}

function CallsTable({ rows, onOpen }: { rows: LlmCall[]; onOpen: (call: LlmCall) => void }) {
  return (
    // 各列不写死宽度，让表格自己按内容分：窄屏上档位、思考 token、日期会折到第二行，不用横着拖
    <Table>
      <thead>
        <tr>
          <Th>时间</Th>
          <Th>环节</Th>
          <Th>档位 · 模型</Th>
          <Th className="text-right">用时</Th>
          <Th className="text-right">输入 token</Th>
          <Th className="text-right">输出 token</Th>
          <Th>结果</Th>
          <Th>属于哪条消息</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((call) => (
          <Tr key={call.id} className="cursor-pointer" onClick={() => onOpen(call)}>
            <Td className="text-muted-foreground text-xs tabular-nums">
              {formatTime(call.started_at)}
            </Td>
            <Td className="whitespace-nowrap">{label(STEP_LABELS, call.purpose)}</Td>
            <Td>
              <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                <span className="font-medium whitespace-nowrap">{call.profile}</span>
                <span className="text-muted-foreground text-xs whitespace-nowrap">
                  · {call.model}
                </span>
                {call.thinking ? (
                  <Badge tone="info">
                    <Brain /> 思考
                  </Badge>
                ) : null}
              </div>
            </Td>
            <Td
              className={cn(
                "text-right whitespace-nowrap tabular-nums",
                call.seconds > SLOW_SECONDS && "text-warning font-medium",
              )}
            >
              {formatSeconds(call.seconds)}
            </Td>
            <Td className="text-right whitespace-nowrap tabular-nums">
              {formatNumber(call.prompt_tokens)}
            </Td>
            <Td className="text-right tabular-nums">
              <span className="whitespace-nowrap">{formatNumber(call.completion_tokens)}</span>
              {call.reasoning_tokens ? (
                <span className="text-muted-foreground inline-block text-xs whitespace-nowrap">
                  （思考 {formatNumber(call.reasoning_tokens)}）
                </span>
              ) : null}
            </Td>
            <Td>
              <Result call={call} />
            </Td>
            <Td className="text-xs whitespace-nowrap">
              {call.trace_id ? (
                <Link
                  to={`/traces/${call.trace_id}`}
                  className="text-brand hover:underline"
                  // 点链接是去看那条消息，不要顺带把这一行的详情也打开
                  onClick={(event) => event.stopPropagation()}
                >
                  看这条消息
                </Link>
              ) : (
                // 不是处理哪条消息时调的（比如在「模型管理」里点的测试）
                <span className="text-muted-foreground">—</span>
              )}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

export default function Calls() {
  const [params, setParams] = useSearchParams();
  const [call, setCall] = useState<LlmCall | null>(null);
  const value = (key: string) => params.get(key) ?? "";
  const set = (key: string, next: string) => {
    const copy = new URLSearchParams(params);
    if (next) copy.set(key, next);
    else copy.delete(key);
    setParams(copy, { replace: true });
  };
  const filters = {
    profile: value("profile") || undefined,
    purpose: value("purpose") || undefined,
    failed: value("failed") === "1",
  };
  const filtered = Boolean(filters.profile || filters.purpose || filters.failed);

  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const profiles = (models.data?.profiles ?? []).map((profile) => profile.name);
  // 网址里带的档位现在的配置里可能已经没有了：照样列出来，不然下拉框是空的
  if (filters.profile && !profiles.includes(filters.profile)) profiles.push(filters.profile);

  const query = useInfiniteQuery({
    queryKey: ["llm-calls", filters],
    queryFn: ({ pageParam }) => api.calls({ ...filters, before: pageParam, limit: PAGE }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) =>
      last.more && last.items.length ? last.items[last.items.length - 1].id : undefined,
    refetchInterval: 15_000,
  });
  const rows = (query.data?.pages ?? []).flatMap((page) => page.items);

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="模型调用"
        description={
          <>
            这里是每一次调用模型的记录，点一行看提示词和回复；统计在
            <Link to="/" className="text-brand hover:underline">
              首页
            </Link>
            。提示词和回复只留一段时间，保留多久在{" "}
            <Link to="/config/core.trace" className="text-brand hover:underline">
              core.trace
            </Link>{" "}
            配置里。
          </>
        }
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
            loading={query.isRefetching}
          >
            {query.isRefetching ? null : <RefreshCw />} 刷新
          </Button>
        }
      />

      <Card>
        <div className="flex flex-wrap items-center gap-2 p-3">
          <SimpleSelect
            size="sm"
            value={value("profile")}
            onChange={(next) => set("profile", next)}
            options={[
              { value: "", label: "档位：全部" },
              ...profiles.map((name) => ({ value: name, label: name })),
            ]}
          />
          <SimpleSelect
            size="sm"
            value={value("purpose")}
            onChange={(next) => set("purpose", next)}
            options={[
              { value: "", label: "环节：全部" },
              ...PURPOSES.map((purpose) => ({
                value: purpose,
                label: label(STEP_LABELS, purpose),
              })),
            ]}
          />
          <Hint text="报错、超时的，以及返回了但不是正常写完的（输出被截断、接口中途放弃）">
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <Switch
                checked={filters.failed}
                onCheckedChange={(on) => set("failed", on ? "1" : "")}
              />
              只看没成的
            </label>
          </Hint>
          {filtered ? (
            <Button variant="ghost" size="sm" onClick={() => setParams({}, { replace: true })}>
              <X /> 清空筛选
            </Button>
          ) : null}
        </div>
      </Card>

      <Card>
        {query.isPending ? (
          <LoadingRows rows={8} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            title={filtered ? "没有符合条件的调用" : "还没有模型调用的记录"}
            hint={
              filtered
                ? "换个筛选条件试试。"
                : "调用记录是从这个功能上线那一刻开始记的。去「网页对话」问一句，这里马上就有。"
            }
          />
        ) : (
          <>
            <CallsTable rows={rows} onOpen={setCall} />
            <div className="text-muted-foreground flex items-center justify-between border-t px-3 py-2 text-xs">
              <span>已显示 {rows.length} 条</span>
              {query.hasNextPage ? (
                <Button
                  variant="outline"
                  size="xs"
                  loading={query.isFetchingNextPage}
                  onClick={() => void query.fetchNextPage()}
                >
                  再看更早的
                </Button>
              ) : (
                <span>没有更早的了</span>
              )}
            </div>
          </>
        )}
      </Card>

      <CallSheet call={call} onClose={() => setCall(null)} />
    </div>
  );
}
