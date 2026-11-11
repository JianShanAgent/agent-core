// 用户与会话 · 右栏：一个会话是谁、它的备注名，以及对话历史 / 记忆 / 处理记录三个标签。

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { Link } from "react-router";

import { CopyButton, EmptyState, ErrorState, LoadingRows } from "@/components/common";
import { TraceTable } from "@/components/TraceTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { label, SCOPE_LABELS } from "@/lib/labels";
import type { SessionDetail as Detail } from "@/lib/types";

import { History } from "./History";
import { Memories } from "./Memories";
import { fromWeb, InlineLabel, isMember, sessionName } from "./shared";

// 接口只给最近这么多条处理记录（routes_data.py）
const TRACE_LIMIT = 20;
// 标签的内容区：占满剩下的高度，自己管滚动
const PANEL = "min-h-0 flex-1 flex-col data-[state=active]:flex";

function BackToList() {
  return (
    <Button asChild variant="ghost" size="xs" className="text-muted-foreground mb-1 -ml-1.5 xl:hidden">
      <Link to="/sessions">
        <ArrowLeft /> 返回列表
      </Link>
    </Button>
  );
}

function Identity({ detail }: { detail: Detail }) {
  // 单聊的详情接口不带昵称：从处理记录里找一个
  const nickname = detail.user_name || detail.traces.find((row) => row.user_name)?.user_name || null;
  const named = { ...detail, user_name: nickname };
  const name = sessionName(named);
  const member = isMember(detail);
  return (
    <div className="border-b px-4 py-3">
      <BackToList />
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="min-w-0 truncate text-base font-semibold">{name}</h2>
        <Badge tone={detail.scope === "group" ? "default" : "muted"}>
          {label(SCOPE_LABELS, detail.scope)}
          {member ? "里的一个成员" : ""}
        </Badge>
        {fromWeb(detail) ? <Badge tone="info">网页对话</Badge> : null}
        {nickname && nickname !== name ? (
          <span className="text-muted-foreground text-xs">昵称 {nickname}</span>
        ) : null}
      </div>
      <div className="text-muted-foreground mt-1 flex min-w-0 items-center gap-1 text-xs">
        <span className="shrink-0">会话键</span>
        <code className="text-foreground/80 min-w-0 font-mono break-all">{detail.session_key}</code>
        <CopyButton text={detail.session_key} label="复制会话键" />
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-6 gap-y-1">
        {member ? (
          <>
            <InlineLabel
              kind="user"
              id={detail.user_id}
              value={detail.user_label}
              name="这个成员的备注名"
            />
            <InlineLabel kind="chat" id={detail.chat_id} value={detail.chat_label} name="群的备注名" />
          </>
        ) : (
          <InlineLabel kind="chat" id={detail.chat_id} value={detail.chat_label} name="备注名" />
        )}
      </div>
    </div>
  );
}

export function SessionDetail({
  sessionKey,
  tab,
  onTab,
}: {
  sessionKey: string;
  tab: string;
  onTab: (tab: string) => void;
}) {
  const query = useQuery({
    queryKey: ["sessions", "detail", sessionKey],
    queryFn: () => api.sessionDetail(sessionKey),
  });
  const detail = query.data;

  if (query.isPending) {
    return (
      <>
        <div className="border-b px-4 py-3">
          <BackToList />
          <code className="text-muted-foreground font-mono text-xs break-all">{sessionKey}</code>
        </div>
        <LoadingRows rows={8} />
      </>
    );
  }
  // 已经有数据时某一次没刷成，照旧显示：不让正在看的对话历史突然变成一句报错
  if (!detail) {
    return (
      <>
        <div className="px-4 pt-3">
          <BackToList />
        </div>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </>
    );
  }

  const traces = detail.traces;
  return (
    <>
      <Identity detail={detail} />
      <Tabs value={tab} onValueChange={onTab} className="min-h-0 flex-1 gap-0">
        <div className="border-b px-4 py-2">
          <TabsList>
            <TabsTrigger value="history">
              对话历史
              <span className="text-muted-foreground text-xs tabular-nums">
                {formatNumber(detail.total_messages)}
              </span>
            </TabsTrigger>
            <TabsTrigger value="memories">
              记忆
              <span className="text-muted-foreground text-xs tabular-nums">
                {formatNumber(detail.memories.length)}
              </span>
            </TabsTrigger>
            <TabsTrigger value="traces">
              处理记录
              <span className="text-muted-foreground text-xs tabular-nums">
                {traces.length >= TRACE_LIMIT ? `${TRACE_LIMIT}+` : traces.length}
              </span>
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="history" className={PANEL}>
          <History sessionKey={sessionKey} detail={detail} />
        </TabsContent>
        <TabsContent value="memories" className={PANEL}>
          <Memories sessionKey={sessionKey} memories={detail.memories} />
        </TabsContent>
        <TabsContent value="traces" className={PANEL}>
          <div className="text-muted-foreground flex flex-wrap items-center justify-between gap-2 border-b px-4 py-1.5 text-xs">
            <span>
              这个会话里每条消息是怎么处理的
              {traces.length >= TRACE_LIMIT ? `。这里只列最近 ${TRACE_LIMIT} 条` : ""}
            </span>
            <Button asChild variant="ghost" size="xs">
              <Link to={`/traces?session=${encodeURIComponent(sessionKey)}`}>
                看全部 <ArrowRight />
              </Link>
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {traces.length ? (
              <TraceTable rows={traces} showWho={false} />
            ) : (
              <EmptyState
                title="这个会话还没有处理记录"
                hint="处理记录是从这个功能上线以后开始记的，更早的对话在这里没有。"
              />
            )}
          </div>
        </TabsContent>
      </Tabs>
    </>
  );
}
