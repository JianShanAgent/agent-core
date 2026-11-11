// 用户与会话 · 对话历史：带给模型的上下文，像聊天记录一样排（用户靠右，机器人靠左）。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Brain, ChevronRight, Eraser, Wrench } from "lucide-react";
import { Fragment, type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { EmptyState, Markdown } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Hint, Pre } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import type { SessionDetail, SessionMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

import { clock, DayDivider, dayKey } from "./shared";

/** 工具的参数是一段 JSON 文字：排整齐了好看；不是 JSON 就原样显示。 */
function prettyArguments(raw: string | null): string {
  if (!raw) return "（没有参数）";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

/** 一行小字，点一下展开下面的原文。 */
function Fold({ summary, children }: { summary: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="w-full max-w-[92%]">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="text-muted-foreground hover:text-foreground flex cursor-pointer items-center gap-1 text-xs"
      >
        <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        {summary}
      </button>
      {open ? <div className="mt-1">{children}</div> : null}
    </div>
  );
}

function Time({ message, children }: { message: SessionMessage; children?: ReactNode }) {
  return (
    <span className="text-muted-foreground flex items-center gap-1 px-1 text-[11px] tabular-nums">
      <span title={formatDateTime(message.created_at)}>{clock(message.created_at)}</span>
      {children}
    </span>
  );
}

function Message({ message }: { message: SessionMessage }) {
  if (message.role === "user") {
    return (
      <div className="flex flex-col items-end gap-0.5">
        <div className="bg-brand/12 max-w-[85%] rounded-2xl rounded-br-md px-3 py-2 text-sm leading-relaxed break-words whitespace-pre-wrap">
          {message.content || <span className="text-muted-foreground">（没有文字）</span>}
        </div>
        <Time message={message} />
      </div>
    );
  }
  if (message.role === "assistant") {
    return (
      <div className="flex flex-col items-start gap-1">
        {message.content ? (
          <div className="bg-muted/60 max-w-[85%] min-w-0 overflow-x-auto rounded-2xl rounded-bl-md border px-3 py-2">
            <Markdown>{message.content}</Markdown>
          </div>
        ) : null}
        {message.tool_calls.map((call, index) => (
          <Fold
            key={index}
            summary={
              <>
                <Wrench className="size-3" /> 调用了工具
                <code className="text-foreground/80 font-mono">{call.name || "（没有名字）"}</code>
              </>
            }
          >
            <Pre>{prettyArguments(call.arguments)}</Pre>
          </Fold>
        ))}
        {!message.content && !message.tool_calls.length ? (
          <div className="text-muted-foreground text-xs">（模型这一条没有正文）</div>
        ) : null}
        <Time message={message}>
          {message.reasoning ? (
            <Hint text="这一条带着模型的思考过程（这里不显示）">
              <Brain className="size-3" />
            </Hint>
          ) : null}
        </Time>
      </div>
    );
  }
  // 工具结果，以及别的不常见的角色：默认折成一行
  return (
    <div className="flex flex-col items-start gap-0.5">
      <Fold
        summary={
          <>
            {message.role === "tool" ? "工具结果" : `${message.role} 消息`}（
            {formatNumber(message.content.length)} 字）
          </>
        }
      >
        <Pre className="max-h-96 overflow-y-auto">{message.content || "（空的）"}</Pre>
      </Fold>
      <Time message={message} />
    </div>
  );
}

export function History({ sessionKey, detail }: { sessionKey: string; detail: SessionDetail }) {
  const client = useQueryClient();
  const [asking, setAsking] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const messages = detail.messages;
  const last = messages.length ? messages[messages.length - 1].id : 0;

  // 像聊天软件一样，打开就停在最新的一条
  useLayoutEffect(() => {
    const element = box.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [last]);

  const clear = useMutation({
    mutationFn: () => api.clearHistory(sessionKey),
    onSuccess: (result) => {
      toast.success(
        result.deleted ? `清空了 ${result.deleted} 条对话历史` : "对话历史本来就是空的",
      );
      setAsking(false);
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error: Error) => {
      toast.error("对话历史没清成", { description: error.message });
    },
  });

  return (
    <>
      <div className="text-muted-foreground flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b px-4 py-1.5 text-xs">
        <span className="min-w-0 flex-1 leading-relaxed">
          这是带给模型的上下文：用户说的、模型回的、中间查的。模型每次只带最近几轮，更早的留在库里但不再带。
          {detail.total_messages > messages.length
            ? `一共 ${formatNumber(detail.total_messages)} 条，这里只显示最近 ${messages.length} 条。`
            : ""}
        </span>
        <Button
          variant="outline"
          size="xs"
          disabled={detail.total_messages === 0}
          onClick={() => setAsking(true)}
        >
          <Eraser /> 清空对话历史
        </Button>
      </div>
      <div ref={box} className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {messages.length ? (
          <div className="flex flex-col gap-2.5">
            {messages.map((message, index) => (
              <Fragment key={message.id}>
                {index === 0 || dayKey(messages[index - 1].created_at) !== dayKey(message.created_at) ? (
                  <DayDivider value={message.created_at} />
                ) : null}
                <Message message={message} />
              </Fragment>
            ))}
          </div>
        ) : (
          <EmptyState
            className="h-full"
            title="没有对话历史"
            hint="这个人还没和机器人聊过，或者他发过 /新话题、历史被清空了。"
          />
        )}
      </div>
      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title="清空这个会话的对话历史？"
        description={
          <>
            等于这个用户自己发了一句 /新话题：机器人会忘掉之前聊到哪了，他的下一句从头开始。
            <b>记忆不受影响</b>，处理记录也还在。删掉的 {formatNumber(detail.total_messages)}{" "}
            条找不回来。
          </>
        }
        confirmText="清空"
        destructive
        loading={clear.isPending}
        onConfirm={() => clear.mutate()}
      />
    </>
  );
}
