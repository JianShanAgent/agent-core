// 查证过程：每一条对机器人说的消息是怎么处理的。这里是列表，点一条进去看每一步。

import { useInfiniteQuery } from "@tanstack/react-query";
import { RefreshCw, Search, X } from "lucide-react";
import { useSearchParams } from "react-router";

import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { TraceTable } from "@/components/TraceTable";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Switch } from "@/components/ui/form";
import { SimpleSelect } from "@/components/ui/select";
import { api, type TraceFilters } from "@/lib/api";
import { useDebounced } from "@/lib/hooks";

const PAGE = 50;
const SLOW_SECONDS = 60;

/** 「结果」这个筛选项对应到接口的哪个参数。 */
const RESULTS: Record<string, Partial<TraceFilters>> = {
  answered: { outcome: "answered" },
  not_found: { outcome: "not_found" },
  failed: { outcome: "failed" },
  error: { status: "error" },
  silent: { status: "silent" },
};

export default function Traces() {
  const [params, setParams] = useSearchParams();
  const value = (key: string) => params.get(key) ?? "";
  const set = (key: string, next: string) => {
    const copy = new URLSearchParams(params);
    if (next) copy.set(key, next);
    else copy.delete(key);
    setParams(copy, { replace: true });
  };
  const search = useDebounced(value("q"));
  const filters: TraceFilters = {
    q: search || undefined,
    route: value("route") || undefined,
    channel: value("channel") || undefined,
    session: value("session") || undefined,
    chat: value("chat") || undefined,
    problems: value("problems") === "1",
    min_seconds: value("slow") === "1" ? SLOW_SECONDS : undefined,
    ...(RESULTS[value("result")] ?? {}),
  };
  const filtered = [...params.keys()].length > 0;

  const query = useInfiniteQuery({
    queryKey: ["traces", filters],
    queryFn: ({ pageParam }) => api.traces({ ...filters, before: pageParam, limit: PAGE }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) =>
      last.more && last.items.length ? last.items[last.items.length - 1].started_at : undefined,
    refetchInterval: 15_000,
  });
  const pages = query.data?.pages ?? [];
  const rows = pages.flatMap((page) => page.items);
  const active = pages[0]?.active ?? [];
  const stored = pages[0]?.stored ?? true;

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="查证过程"
        description="每一条对机器人说的消息是怎么处理的：走了哪条路、每一步花了多久、看了哪些资料、复核说了什么。"
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
          <div className="relative min-w-52 flex-1">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
            <Input
              className="h-8 pl-8"
              placeholder="搜问题或回复里的字"
              value={value("q")}
              onChange={(event) => set("q", event.target.value)}
            />
          </div>
          <SimpleSelect
            size="sm"
            value={value("route")}
            onChange={(next) => set("route", next)}
            options={[
              { value: "", label: "走哪条路：全部" },
              { value: "research", label: "查证" },
              { value: "chat", label: "闲聊" },
              { value: "command", label: "命令" },
              { value: "limited", label: "被限流" },
            ]}
          />
          <SimpleSelect
            size="sm"
            value={value("result")}
            onChange={(next) => set("result", next)}
            options={[
              { value: "", label: "结果：全部" },
              { value: "answered", label: "查到了" },
              { value: "not_found", label: "没查到" },
              { value: "failed", label: "查证失败" },
              { value: "error", label: "处理出错" },
              { value: "silent", label: "没回复" },
            ]}
          />
          <SimpleSelect
            size="sm"
            value={value("channel")}
            onChange={(next) => set("channel", next)}
            options={[
              { value: "", label: "来源：全部" },
              { value: "qq", label: "QQ" },
              { value: "web", label: "网页对话" },
            ]}
          />
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <Switch
              checked={value("problems") === "1"}
              onCheckedChange={(on) => set("problems", on ? "1" : "")}
            />
            只看有问题的
          </label>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <Switch
              checked={value("slow") === "1"}
              onCheckedChange={(on) => set("slow", on ? "1" : "")}
            />
            超过 {SLOW_SECONDS} 秒的
          </label>
          {filtered ? (
            <Button variant="ghost" size="sm" onClick={() => setParams({}, { replace: true })}>
              <X /> 清空筛选
            </Button>
          ) : null}
        </div>
        {value("session") || value("chat") ? (
          <div className="text-muted-foreground border-t px-3 py-2 text-xs">
            只看{value("session") ? "这个会话" : "这个群"}的：
            <code className="font-mono">{value("session") || value("chat")}</code>
          </div>
        ) : null}
      </Card>

      {active.length ? (
        <Card className="border-info/40">
          <CardHeader>
            <CardTitle>正在处理 {active.length} 条</CardTitle>
          </CardHeader>
          <TraceTable rows={active} />
        </Card>
      ) : null}

      <Card>
        {query.isPending ? (
          <LoadingRows rows={8} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            title={filtered ? "没有符合条件的记录" : "还没有处理记录"}
            hint={
              filtered
                ? "换个筛选条件试试。"
                : "处理记录是从这个功能上线那一刻开始记的。去「网页对话」发一句，这里马上就有。"
            }
          />
        ) : (
          <>
            <TraceTable rows={rows} />
            <div className="text-muted-foreground flex items-center justify-between border-t px-3 py-2 text-xs">
              <span>
                已显示 {rows.length} 条
                {stored ? "" : "（没有数据库：只有内存里最近的几十条，重启就没了）"}
              </span>
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
    </div>
  );
}
