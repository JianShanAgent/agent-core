// 群聊记录 · 右栏：一个群的聊天记录。旧的在上、新的在下，往上翻是更早的。

import { useInfiniteQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, AtSign, Paperclip, RefreshCw } from "lucide-react";
import { Fragment, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

import { CopyButton, EmptyState, ErrorState, LoadingRows } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/form";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { useDebounced } from "@/lib/hooks";
import { label, SCOPE_LABELS, who } from "@/lib/labels";
import type { ChatLogMessage, ChatSummary } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  chatName,
  clock,
  DayDivider,
  dayKey,
  InlineLabel,
  LabelDialog,
  SearchBox,
} from "@/pages/sessions/shared";

const PAGE = 100;
const AVATAR_COLORS = 6;

/** 同一个人每次都是同一种颜色（主题里图表用的那六种），一眼能分出谁是谁。 */
function avatarColor(userId: string): string {
  let hash = 0;
  for (let index = 0; index < userId.length; index += 1) {
    hash = (hash * 31 + userId.charCodeAt(index)) >>> 0;
  }
  return `var(--chart-${(hash % AVATAR_COLORS) + 1})`;
}

function MessageRow({ message, onName }: { message: ChatLogMessage; onName: () => void }) {
  const name = who(message);
  const color = avatarColor(message.user_id);
  return (
    <div className={cn("flex gap-2.5 px-4 py-1.5", message.addressed && "bg-brand/8")}>
      <span
        className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full text-xs font-medium"
        style={{ color, backgroundColor: `color-mix(in oklch, ${color} 16%, transparent)` }}
        aria-hidden
      >
        {[...name][0] ?? "?"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <button
            type="button"
            onClick={onName}
            title="点一下给他起备注名"
            className="hover:text-brand max-w-full cursor-pointer truncate text-[13px] font-medium hover:underline"
          >
            {name}
          </button>
          <span
            className="text-muted-foreground text-[11px] tabular-nums"
            title={formatDateTime(message.sent_at)}
          >
            {clock(message.sent_at)}
          </span>
          {message.addressed ? (
            <Badge tone="brand">
              <AtSign /> 对机器人说的
            </Badge>
          ) : null}
        </div>
        <div className="text-sm leading-relaxed break-words whitespace-pre-wrap">
          {message.content || <span className="text-muted-foreground">（没有文字）</span>}
        </div>
        {message.attachments > 0 ? (
          <div className="text-muted-foreground mt-0.5 flex items-center gap-1 text-xs">
            <Paperclip className="size-3" /> 带了 {message.attachments} 个附件
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function ChatMessages({
  chatId,
  chat,
  days,
  label: fallbackLabel,
}: {
  chatId: string;
  /** 这个群在列表里的那一行（列表还没取到，或者群不在列表里时没有） */
  chat: ChatSummary | undefined;
  /** 列表是按最近多少天取的（0 = 全部）：chat 里的条数、人数只算这段时间里的 */
  days: number;
  /** 群不在列表里（这段时间没人说话）时，另外取到的备注名 */
  label?: string;
}) {
  const [text, setText] = useState("");
  const [addressed, setAddressed] = useState(false);
  const [naming, setNaming] = useState<ChatLogMessage | null>(null);
  const q = useDebounced(text.trim());
  const filtered = q !== "" || addressed;

  const query = useInfiniteQuery({
    queryKey: ["chatlog", "messages", chatId, { q, addressed }],
    queryFn: ({ pageParam }) =>
      api.chatMessages(chatId, { q: q || undefined, addressed, limit: PAGE, before: pageParam }),
    initialPageParam: undefined as number | undefined,
    // 接口给的是新的在前：每一页最后一条是最旧的，拿它的编号去取更早的
    getNextPageParam: (last) =>
      last.more && last.items.length ? last.items[last.items.length - 1].id : undefined,
    placeholderData: (previous) => previous,
  });
  const pages = query.data?.pages ?? [];
  // 显示时倒过来：旧的在上
  const rows = pages.flatMap((page) => page.items).reverse();

  // ---- 滚动 ----
  const box = useRef<HTMLDivElement>(null);
  // 取更早的那一刻内容有多高、滚到哪：新内容加在上面以后，按差值补回去，眼前的那条不动
  const anchor = useRef<{ height: number; top: number } | null>(null);
  // 已经为哪一组筛选条件滚到过底
  const settled = useRef("");
  const view = `${q}\n${addressed}`;
  const newest = rows.length ? rows[rows.length - 1].id : 0;
  const stale = query.isPlaceholderData;

  useLayoutEffect(() => {
    const element = box.current;
    if (!element || stale || !rows.length) return;
    if (anchor.current) {
      element.scrollTop = anchor.current.top + (element.scrollHeight - anchor.current.height);
      anchor.current = null;
    } else if (settled.current !== view) {
      // 刚打开，或者换了筛选条件：像聊天软件一样停在最新的一条
      element.scrollTop = element.scrollHeight;
      settled.current = view;
    }
  }, [pages.length, rows.length, newest, view, stale]);

  const loadOlder = () => {
    const element = box.current;
    if (element) anchor.current = { height: element.scrollHeight, top: element.scrollTop };
    void query.fetchNextPage().then((result) => {
      if (!result.isError) return;
      // 没取到：记下的位置作废，免得之后别的刷新拿它去挪滚动条
      anchor.current = null;
      toast.error("更早的消息没取到", { description: result.error.message });
    });
  };
  const refresh = () => {
    // 刷新是为了看新消息：取完回到最底下
    settled.current = "";
    void query.refetch().then((result) => {
      if (result.isError) toast.error("没刷新成", { description: result.error.message });
    });
  };

  const chatLabel = chat?.chat_label ?? fallbackLabel ?? "";
  const name = chatName({ chat_id: chatId, chat_label: chatLabel });

  return (
    <>
      <div className="border-b px-4 py-3">
        <Button
          asChild
          variant="ghost"
          size="xs"
          className="text-muted-foreground mb-1 -ml-1.5 xl:hidden"
        >
          <Link to="/chatlog">
            <ArrowLeft /> 返回列表
          </Link>
        </Button>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="min-w-0 truncate text-base font-semibold">{name}</h2>
          {chat ? <Badge tone="default">{label(SCOPE_LABELS, chat.scope)}</Badge> : null}
          {chat ? (
            <span className="text-muted-foreground text-xs">
              {days ? `最近 ${days} 天` : "共"} {formatNumber(chat.messages)} 条 ·{" "}
              {formatNumber(chat.members)} 人发过言
            </span>
          ) : null}
          <Button asChild variant="ghost" size="xs" className="ml-auto">
            <Link to={`/traces?chat=${encodeURIComponent(chatId)}`}>
              看这个群的处理记录 <ArrowRight />
            </Link>
          </Button>
        </div>
        <div className="text-muted-foreground mt-1 flex min-w-0 items-center gap-1 text-xs">
          <span className="shrink-0">群标识</span>
          <code className="text-foreground/80 min-w-0 font-mono break-all">{chatId}</code>
          <CopyButton text={chatId} label="复制群标识" />
        </div>
        <div className="mt-1.5">
          <InlineLabel kind="chat" id={chatId} value={chatLabel} name="备注名" />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-2">
        <SearchBox
          className="min-w-48 flex-1"
          placeholder="搜消息里的字、昵称"
          value={text}
          onChange={setText}
        />
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Switch checked={addressed} onCheckedChange={setAddressed} />
          只看对机器人说的
        </label>
        <Button
          variant="outline"
          size="sm"
          onClick={refresh}
          loading={query.isRefetching && !query.isFetchingNextPage}
        >
          {query.isRefetching && !query.isFetchingNextPage ? null : <RefreshCw />} 刷新
        </Button>
      </div>

      <div ref={box} className="min-h-0 flex-1 overflow-y-auto">
        {query.isPending ? (
          <LoadingRows rows={8} />
        ) : !query.data ? (
          // 已经有记录时后来的一次没取到（往前翻、刷新），记录照旧显示，另外用提示说一声
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            className="h-full"
            title={filtered ? "没有符合条件的消息" : "这个群还没有记录"}
            hint={
              filtered
                ? "换个词，或者把「只看对机器人说的」关掉。"
                : "群聊记录是从这个功能上线以后开始记的；没让机器人「获取群内全部消息」的群，只记 @ 它的消息。"
            }
          />
        ) : (
          <div className={cn("py-2 transition-opacity", stale && "opacity-50")}>
            <div className="text-muted-foreground flex justify-center pb-1 text-xs">
              {query.hasNextPage ? (
                <Button
                  variant="outline"
                  size="xs"
                  loading={query.isFetchingNextPage}
                  disabled={stale}
                  onClick={loadOlder}
                >
                  再看更早的
                </Button>
              ) : (
                <span className="py-1">{filtered ? "符合条件的就这些" : "这是最早的一条了"}</span>
              )}
            </div>
            {rows.map((message, index) => (
              <Fragment key={message.id}>
                {index === 0 || dayKey(rows[index - 1].sent_at) !== dayKey(message.sent_at) ? (
                  <DayDivider value={message.sent_at} className="px-4" />
                ) : null}
                <MessageRow message={message} onName={() => setNaming(message)} />
              </Fragment>
            ))}
          </div>
        )}
      </div>
      {rows.length ? (
        <div className="text-muted-foreground border-t px-4 py-1.5 text-xs">
          已显示 {formatNumber(rows.length)} 条{filtered ? "（筛选后）" : ""}
        </div>
      ) : null}

      <LabelDialog
        target={
          naming
            ? {
                kind: "user",
                id: naming.user_id,
                label: naming.user_label,
                nickname: naming.user_name,
              }
            : null
        }
        onClose={() => setNaming(null)}
      />
    </>
  );
}
