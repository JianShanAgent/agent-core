// 群聊记录：机器人在群里收到的每一条消息（只旁听、只存库）。左边挑群，右边看记录。
// 左边默认只列最近 7 天有消息的群，条数、人数也只算这 7 天的：这一栏开着就会每半分钟刷一次，
// 群聊记录只增不减，每次都把整张表算一遍会越来越慢。要看全部的把范围换成「全部」（不自动刷新）。

import { useQuery } from "@tanstack/react-query";
import { MessagesSquare, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";

import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { FullHeight } from "@/components/layout/AppShell";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import { formatAgo, formatNumber, formatTime } from "@/lib/format";
import type { ChatSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

import { ChatMessages } from "./chatlog/ChatMessages";
import { chatName, TwoPane } from "./sessions/shared";

/** 列多久以内有消息的群。0 = 不限时间。 */
const RANGES = [
  { value: "1", label: "最近 1 天" },
  { value: "7", label: "最近 7 天" },
  { value: "30", label: "最近 30 天" },
  { value: "0", label: "全部（慢一些，不自动刷新）" },
];

/** 条数、人数前面的那几个字：这些数只算清单选的那段时间里的。 */
const rangeWords = (days: number): string => (days ? `最近 ${days} 天` : "共");

function ChatItem({
  chat,
  days,
  active,
  onSelect,
}: {
  chat: ChatSummary;
  /** 清单是按最近多少天取的（0 = 全部）：下面的条数、人数只算这段时间里的 */
  days: number;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "hover:bg-muted/60 flex w-full cursor-pointer flex-col gap-1 border-b px-3 py-2.5 text-left transition-colors outline-none focus-visible:bg-muted/60",
        active && "bg-accent hover:bg-accent shadow-[inset_2px_0_0_var(--brand)]",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{chatName(chat)}</span>
        <span className="text-muted-foreground shrink-0 text-xs">{formatAgo(chat.last_at)}</span>
      </div>
      <div className="text-muted-foreground text-xs">
        {rangeWords(days)} {formatNumber(chat.messages)} 条 · 对机器人说的{" "}
        {formatNumber(chat.addressed)} 条 · {formatNumber(chat.members)} 人发过言
      </div>
      <div className="text-muted-foreground text-xs">
        {days === 1 ? null : <>最近 24 小时 {formatNumber(chat.last_day)} 条 · </>}最后一条在{" "}
        {formatTime(chat.last_at, { seconds: false })}
      </div>
    </button>
  );
}

export default function ChatLog() {
  // 地址里是 /chatlog/<群标识>
  const { chat: chatId } = useParams();
  const navigate = useNavigate();
  const [range, setRange] = useState("7");
  const wanted = Number(range);
  const query = useQuery({
    queryKey: ["chatlog", "chats", wanted],
    queryFn: () => api.chats(wanted),
    placeholderData: (previous) => previous,
    // 「全部」要把整张表过一遍，不自动刷新，旁边有刷新按钮
    refetchInterval: wanted === 0 ? false : 30_000,
  });
  const chats = query.data?.items ?? [];
  // 现在显示的这一批是按多少天取的（刚换了范围、新的还没取回来时，显示的还是上一批）
  const days = query.data?.days ?? wanted;
  const selected = chats.find((item) => item.chat_id === chatId);
  // 选中的群不在这段时间的清单里（这几天没人说话）：它的备注名另外取，右边照样看得了记录
  const labels = useQuery({
    queryKey: ["chatlog", "labels"],
    queryFn: api.labels,
    enabled: Boolean(chatId) && query.data !== undefined && !selected,
  });

  return (
    <FullHeight>
      <PageHeader
        title="群聊记录"
        description="机器人在群里收到的消息。它只旁听、只存库，回答问题时不读这些；只有 @ 了它的消息它才会回。"
      />
      <TwoPane
        selected={Boolean(chatId)}
        list={
          <>
            <div className="flex items-center gap-2 border-b p-3">
              <SimpleSelect
                size="sm"
                className="min-w-0 flex-1"
                value={range}
                onChange={setRange}
                options={RANGES}
              />
              {wanted === 0 ? (
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
              <span className="text-muted-foreground flex shrink-0 items-center gap-1.5 text-xs">
                {query.isFetching && query.data && wanted !== 0 ? (
                  <Spinner className="size-3.5" />
                ) : null}
                {query.data ? `${chats.length} 个群` : ""}
              </span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {query.isPending ? (
                <LoadingRows rows={6} />
              ) : !query.data ? (
                // 列表会自己刷新：已经有数据时某一次没刷成，照旧显示，不换成报错
                <ErrorState error={query.error} onRetry={() => void query.refetch()} />
              ) : chats.length === 0 && days ? (
                <EmptyState
                  title={`最近 ${days} 天没有群聊消息`}
                  hint="更早的记录还在：把上面的范围换成「全部」就能看到。"
                />
              ) : chats.length === 0 ? (
                <EmptyState
                  title="还没有群聊记录"
                  hint="群聊记录是从这个功能上线以后开始记的。群主让机器人「获取群内全部消息」的群，每一条发言都记；其余的群只记 @ 机器人的。网页对话里发的不记在这里。"
                />
              ) : (
                chats.map((chat) => (
                  <ChatItem
                    key={chat.chat_id}
                    chat={chat}
                    days={days}
                    active={chat.chat_id === chatId}
                    onSelect={() => navigate(`/chatlog/${encodeURIComponent(chat.chat_id)}`)}
                  />
                ))
              )}
            </div>
          </>
        }
        detail={
          chatId ? (
            <ChatMessages
              key={chatId}
              chatId={chatId}
              chat={selected}
              days={days}
              label={labels.data?.chat[chatId]}
            />
          ) : (
            <EmptyState
              className="flex-1"
              icon={MessagesSquare}
              title="选一个群看它的聊天记录"
              hint="对机器人说的消息会标出来。点一个人的名字可以给他起备注名。"
            />
          )
        }
      />
    </FullHeight>
  );
}
