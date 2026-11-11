// 用户与会话 · 记忆：机器人记住的关于这个人的事，可以一条条删，也可以全部清空。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eraser, NotebookPen, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Hint } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatDateTime, formatTime } from "@/lib/format";
import type { SessionDetail } from "@/lib/types";

type Memory = SessionDetail["memories"][number];

export function Memories({ sessionKey, memories }: { sessionKey: string; memories: Memory[] }) {
  const client = useQueryClient();
  const [target, setTarget] = useState<Memory | null>(null);
  const [clearing, setClearing] = useState(false);
  const refresh = () => void client.invalidateQueries({ queryKey: ["sessions"] });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteMemory(id),
    onSuccess: (result) => {
      toast.success(result.deleted ? "删掉了这条记忆" : "这条记忆已经不在了");
      setTarget(null);
      refresh();
    },
    onError: (error: Error) => {
      toast.error("这条记忆没删成", { description: error.message });
    },
  });
  const clear = useMutation({
    mutationFn: () => api.clearMemories(sessionKey),
    onSuccess: (result) => {
      toast.success(`清空了 ${result.deleted} 条记忆`);
      setClearing(false);
      refresh();
    },
    onError: (error: Error) => {
      toast.error("记忆没清成", { description: error.message });
    },
  });

  return (
    <>
      <div className="text-muted-foreground flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b px-4 py-1.5 text-xs">
        <span className="min-w-0 flex-1 leading-relaxed">
          机器人记住的关于这个人的事，和他对话时会带上。清空对话历史不会动这里。
        </span>
        <Button
          variant="outline"
          size="xs"
          disabled={memories.length === 0}
          onClick={() => setClearing(true)}
        >
          <Eraser /> 全部清空
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {memories.length ? (
          memories.map((memory) => (
            <div
              key={memory.id}
              className="hover:bg-muted/40 flex items-start gap-3 border-b px-4 py-2.5 transition-colors"
            >
              <div className="min-w-0 flex-1 text-sm leading-relaxed break-words whitespace-pre-wrap">
                {memory.content}
              </div>
              <span
                className="text-muted-foreground shrink-0 pt-0.5 text-xs tabular-nums"
                title={formatDateTime(memory.created_at)}
              >
                {formatTime(memory.created_at, { seconds: false })}
              </span>
              <Hint text="删掉这一条">
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="text-muted-foreground hover:text-destructive -my-0.5"
                  onClick={() => setTarget(memory)}
                >
                  <Trash2 />
                  <span className="sr-only">删掉这一条</span>
                </Button>
              </Hint>
            </div>
          ))
        ) : (
          <EmptyState
            className="h-full"
            icon={NotebookPen}
            title="没有记忆"
            hint="机器人还没有记住关于这个人的事。群里只记用户明确要求记住的。"
          />
        )}
      </div>
      <ConfirmDialog
        open={target !== null}
        onOpenChange={(open) => !open && setTarget(null)}
        title="删掉这条记忆？"
        description={
          <>
            <div className="bg-muted/60 text-foreground max-h-40 overflow-y-auto rounded-md border px-3 py-2 break-words whitespace-pre-wrap">
              {target?.content}
            </div>
            <div className="mt-2">删了以后机器人就不记得这件事了，找不回来。</div>
          </>
        }
        confirmText="删掉"
        destructive
        loading={remove.isPending}
        onConfirm={() => target && remove.mutate(target.id)}
      />
      <ConfirmDialog
        open={clearing}
        onOpenChange={setClearing}
        title={`清空这个会话的全部 ${memories.length} 条记忆？`}
        description="机器人会忘掉记住的关于这个人的所有事，找不回来。对话历史不受影响。"
        confirmText="全部清空"
        destructive
        loading={clear.isPending}
        onConfirm={() => clear.mutate()}
      />
    </>
  );
}
