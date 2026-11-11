// 用户与会话 · 左栏：会话列表，最近活动的在前。
// 默认只列最近 30 天有动静的会话：这一栏开着就会每半分钟刷一次，对话历史、处理记录这几张表
// 只增不减，每次都把它们整个算一遍会越来越慢。要看更早的，把范围换成「全部」（不自动刷新）。

import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState, LoadingRows, Pager } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import { formatAgo, formatNumber } from "@/lib/format";
import { useDebounced } from "@/lib/hooks";
import { label, SCOPE_LABELS } from "@/lib/labels";
import type { SessionRow } from "@/lib/types";
import { cn } from "@/lib/utils";

import { chatName, fromWeb, isMember, SearchBox, sessionName, useOffset } from "./shared";

const PAGE = 50;

/** 列多久以内有动静的会话。0 = 不限时间。 */
const RANGES = [
  { value: "7", label: "最近 7 天有动静的" },
  { value: "30", label: "最近 30 天有动静的" },
  { value: "90", label: "最近 90 天有动静的" },
  { value: "0", label: "全部（慢一些，不自动刷新）" },
];

function Item({
  row,
  active,
  onSelect,
}: {
  row: SessionRow;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "hover:bg-muted/60 flex w-full cursor-pointer flex-col gap-1 border-b px-3 py-2.5 text-left transition-colors outline-none focus-visible:bg-muted/60",
        // 选中的那一项：底色加左边一道线
        active && "bg-accent hover:bg-accent shadow-[inset_2px_0_0_var(--brand)]",
      )}
    >
      <div className="flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{sessionName(row)}</span>
        {fromWeb(row) ? <Badge tone="info">网页</Badge> : null}
        <Badge tone={row.scope === "group" ? "default" : "muted"}>
          {label(SCOPE_LABELS, row.scope)}
        </Badge>
      </div>
      {isMember(row) ? (
        <div className="text-muted-foreground truncate text-xs">在群「{chatName(row)}」里</div>
      ) : null}
      <div className="text-muted-foreground flex items-center justify-between gap-2 text-xs">
        <span className="truncate">
          {formatNumber(row.turns)} 轮对话 · {formatNumber(row.memories)} 条记忆 ·{" "}
          {formatNumber(row.traces)} 条处理记录
        </span>
        <span className="shrink-0">{row.last_at ? formatAgo(row.last_at) : "—"}</span>
      </div>
    </button>
  );
}

export function SessionList({
  selected,
  onSelect,
}: {
  selected: string | undefined;
  onSelect: (sessionKey: string) => void;
}) {
  const [text, setText] = useState("");
  const [range, setRange] = useState("30");
  const days = Number(range);
  const q = useDebounced(text.trim());
  // 换了搜索词或者时间范围都回到第一页
  const [offset, setOffset] = useOffset(`${range}\n${q}`);
  const query = useQuery({
    queryKey: ["sessions", "list", { q, offset, days }],
    queryFn: () => api.sessions({ q: q || undefined, limit: PAGE, offset, days }),
    placeholderData: (previous) => previous,
    // 「全部」要把几张表整个过一遍，不自动刷新，旁边有刷新按钮
    refetchInterval: days === 0 ? false : 30_000,
  });
  const data = query.data;

  return (
    <>
      <div className="flex flex-col gap-2 border-b p-3">
        <div className="flex items-center gap-2">
          <SearchBox
            className="min-w-0 flex-1"
            placeholder="搜会话键、昵称"
            value={text}
            onChange={setText}
          />
          <span className="text-muted-foreground flex shrink-0 items-center gap-1.5 text-xs">
            {query.isFetching && data ? <Spinner className="size-3.5" /> : null}
            {data ? `${formatNumber(data.total)} 个` : ""}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SimpleSelect
            size="sm"
            className="min-w-0 flex-1"
            value={range}
            onChange={setRange}
            options={RANGES}
          />
          {days === 0 ? (
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0"
              loading={query.isRefetching}
              onClick={() => void query.refetch()}
            >
              {query.isRefetching ? null : <RefreshCw />} 刷新
            </Button>
          ) : null}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {query.isPending ? (
          <LoadingRows rows={8} />
        ) : !data ? (
          // 列表会自己刷新：已经有数据时某一次没刷成（比如机器人在重启），照旧显示，不换成报错
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : data.items.length === 0 ? (
          q ? (
            <EmptyState
              title="没有符合条件的会话"
              hint={
                "搜的是会话键和昵称。备注名搜不到：按备注名找人的话，直接在列表里翻。" +
                (days ? `现在只在最近 ${days} 天有动静的会话里找，更早的把上面的范围换成「全部」。` : "")
              }
            />
          ) : days ? (
            <EmptyState
              title={`最近 ${days} 天没有会话有动静`}
              hint="更早的会话还在：把上面的范围换成「全部」就能看到。"
            />
          ) : (
            <EmptyState title="还没有会话" hint="有人和机器人说过话以后，这里才有。" />
          )
        ) : (
          data.items.map((row) => (
            <Item
              key={row.session_key}
              row={row}
              active={row.session_key === selected}
              onSelect={() => onSelect(row.session_key)}
            />
          ))
        )}
      </div>
      {data && data.total > PAGE ? (
        <div className="border-t">
          <Pager offset={offset} limit={PAGE} total={data.total} onChange={setOffset} />
        </div>
      ) : null}
    </>
  );
}
