// 一次模型调用的详情：发给模型的提示词、它的思考和回复。从右边滑出来。

import { useQuery } from "@tanstack/react-query";
import { Brain } from "lucide-react";
import { useState } from "react";

import { CopyButton, EmptyState, ErrorState, LoadingRows } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle, SheetContent } from "@/components/ui/dialog";
import { Pre } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber, formatSeconds } from "@/lib/format";
import { label, STEP_LABELS } from "@/lib/labels";
import type { ChatMessagePart, LlmCall } from "@/lib/types";

const ROLE_NAMES: Record<string, string> = {
  system: "系统提示",
  user: "用户",
  assistant: "模型",
  tool: "工具结果",
};
const COLLAPSE_CHARS = 2000;

/** 长文字先只显示开头，点一下展开。 */
export function LongText({ text, className }: { text: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > COLLAPSE_CHARS;
  return (
    <div className="relative">
      <Pre className={className}>{long && !open ? `${text.slice(0, COLLAPSE_CHARS)}…` : text}</Pre>
      <div className="absolute top-1 right-1">
        <CopyButton text={text} />
      </div>
      {long ? (
        <Button variant="link" size="xs" className="mt-0.5 px-0" onClick={() => setOpen(!open)}>
          {open ? "收起" : `展开全部（共 ${formatNumber(text.length)} 字）`}
        </Button>
      ) : null}
    </div>
  );
}

function MessageBlock({ message }: { message: ChatMessagePart }) {
  const calls = message.tool_calls ?? [];
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <Badge tone={message.role === "system" ? "muted" : "default"}>
          {ROLE_NAMES[message.role] ?? message.role}
        </Badge>
        {message.content ? (
          <span className="text-muted-foreground text-[11px]">
            {formatNumber(message.content.length)} 字
          </span>
        ) : null}
      </div>
      {message.reasoning_content ? (
        <LongText text={message.reasoning_content} className="mb-1.5 border-dashed opacity-80" />
      ) : null}
      {message.content ? <LongText text={message.content} /> : null}
      {calls.map((call, index) => (
        <Pre key={call.id ?? index} className="mt-1.5">
          调用工具 {call.function?.name}({call.function?.arguments})
        </Pre>
      ))}
    </div>
  );
}

export function CallMeta({ call }: { call: LlmCall }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <Badge tone="outline">{label(STEP_LABELS, call.purpose)}</Badge>
      <Badge tone="default">
        {call.profile} · {call.model}
      </Badge>
      {call.thinking ? (
        <Badge tone="info">
          <Brain /> 开思考
        </Badge>
      ) : null}
      <Badge tone={call.ok ? "muted" : "danger"}>{formatSeconds(call.seconds)}</Badge>
      <span className="text-muted-foreground">
        输入 {formatNumber(call.prompt_tokens)} · 输出 {formatNumber(call.completion_tokens)}
        {call.reasoning_tokens ? `（思考 ${formatNumber(call.reasoning_tokens)}）` : ""}
        {call.cached_tokens ? ` · 缓存命中 ${formatNumber(call.cached_tokens)}` : ""}
      </span>
      {call.ok && call.finish_reason && !["stop", "tool_calls"].includes(call.finish_reason) ? (
        <Badge tone="warning">结束原因 {call.finish_reason}</Badge>
      ) : null}
    </div>
  );
}

export function CallSheet({
  call,
  onClose,
}: {
  call: LlmCall | null;
  onClose: () => void;
}) {
  const query = useQuery({
    queryKey: ["llm-call", call?.id, call?.trace_id],
    queryFn: () => api.call(call!.id, call!.trace_id),
    enabled: call !== null,
  });
  const detail = query.data;
  return (
    <Dialog open={call !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent>
        {call ? (
          <>
            <div className="border-b px-5 py-4 pr-12">
              <DialogTitle>
                一次模型调用 · {label(STEP_LABELS, call.purpose)}
              </DialogTitle>
              <DialogDescription className="mt-0.5 text-xs">
                {formatDateTime(call.started_at)}
              </DialogDescription>
              <div className="mt-2.5">
                <CallMeta call={call} />
              </div>
              {call.error ? (
                <div className="text-destructive bg-destructive/8 mt-2 rounded-md px-2.5 py-1.5 text-xs">
                  没成：{call.error}
                </div>
              ) : null}
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {query.isPending ? (
                <LoadingRows />
              ) : query.isError ? (
                <ErrorState error={query.error} onRetry={() => void query.refetch()} />
              ) : !detail?.request && !detail?.response ? (
                <EmptyState
                  title="这次调用的提示词和回复没有留"
                  hint="要么过了保留期被清掉了，要么配置里关了「留提示词」（core.trace.yaml 的 prompts）。用量数字还在。"
                />
              ) : (
                <Tabs defaultValue={detail.response ? "response" : "request"}>
                  <TabsList>
                    <TabsTrigger value="response">模型的回复</TabsTrigger>
                    <TabsTrigger value="request">
                      发给模型的（{detail.request?.messages.length ?? 0} 条）
                    </TabsTrigger>
                  </TabsList>
                  <TabsContent value="response" className="flex flex-col gap-4">
                    {detail.response ? (
                      <>
                        {detail.response.reasoning ? (
                          <div>
                            <div className="text-muted-foreground mb-1 flex items-center gap-1.5 text-xs">
                              <Brain className="size-3.5" /> 思考过程
                            </div>
                            <LongText
                              text={detail.response.reasoning}
                              className="border-dashed opacity-85"
                            />
                          </div>
                        ) : null}
                        <div>
                          <div className="text-muted-foreground mb-1 text-xs">正文</div>
                          {detail.response.content ? (
                            <LongText text={detail.response.content} />
                          ) : (
                            <div className="text-muted-foreground text-sm">（正文是空的）</div>
                          )}
                        </div>
                        {detail.response.tool_calls.length ? (
                          <div>
                            <div className="text-muted-foreground mb-1 text-xs">调用了工具</div>
                            {detail.response.tool_calls.map((item, index) => (
                              <Pre key={item.id ?? index} className="mb-1.5">
                                {item.function?.name}({item.function?.arguments})
                              </Pre>
                            ))}
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <EmptyState title="没有回复" hint="这次调用没成，模型什么都没返回。" />
                    )}
                  </TabsContent>
                  <TabsContent value="request" className="flex flex-col gap-4">
                    {detail.request ? (
                      <>
                        <div className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 text-xs">
                          {detail.request.tools.length ? (
                            <span>给了工具：{detail.request.tools.join("、")}</span>
                          ) : (
                            <span>没给工具</span>
                          )}
                          {detail.request.json_mode ? <span>要求只返回 JSON</span> : null}
                        </div>
                        {detail.request.messages.map((message, index) => (
                          <MessageBlock key={index} message={message} />
                        ))}
                      </>
                    ) : (
                      <EmptyState title="提示词没有留" />
                    )}
                  </TabsContent>
                </Tabs>
              )}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Dialog>
  );
}
