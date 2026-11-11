// 知识库里一个切片的全文（查证过程里的资料、知识库页的列表都点得进来）。

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";

import { CopyButton, ErrorState, LoadingRows } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Pre } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { KB_TYPE_LABELS, label } from "@/lib/labels";

export function ChunkDialog({
  chunkId,
  onClose,
}: {
  chunkId: string | null;
  onClose: () => void;
}) {
  // 在同一篇文档里前后翻的时候，对话框不关
  const [current, setCurrent] = useState(chunkId);
  useEffect(() => setCurrent(chunkId), [chunkId]);
  const query = useQuery({
    queryKey: ["kb", "chunk", current],
    queryFn: () => api.kbChunk(current!),
    enabled: current !== null,
    placeholderData: (previous) => previous,
  });
  const chunk = query.data;
  const position = chunk ? chunk.siblings.findIndex((item) => item.chunk_id === chunk.chunk_id) : -1;
  const previous = chunk && position > 0 ? chunk.siblings[position - 1] : null;
  const next =
    chunk && position >= 0 && position < chunk.siblings.length - 1
      ? chunk.siblings[position + 1]
      : null;
  const certain = chunk?.entities.filter((entity) => entity.certain !== false) ?? [];

  return (
    <Dialog open={chunkId !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        {query.isPending ? (
          <>
            <DialogTitle>切片</DialogTitle>
            <LoadingRows />
          </>
        ) : query.isError || !chunk ? (
          <>
            <DialogTitle>切片</DialogTitle>
            <ErrorState error={query.error} />
          </>
        ) : (
          <>
            <div className="pr-8">
              <DialogTitle className="flex flex-wrap items-center gap-2">
                {chunk.title}
                <Badge tone="outline">{label(KB_TYPE_LABELS, chunk.type)}</Badge>
                {chunk.has_vector ? null : <Badge tone="warning">没有向量</Badge>}
              </DialogTitle>
              <DialogDescription className="mt-1 text-xs">
                {chunk.section_path.length ? `${chunk.section_path.join(" › ")} · ` : ""}
                {chunk.date
                  ? `日期 ${chunk.date} · `
                  : chunk.date_inferred
                    ? `日期 ${chunk.date_inferred}（推断）· `
                    : ""}
                {formatNumber(chunk.text.length)} 字 · 导入于 {formatDateTime(chunk.updated_at)}
              </DialogDescription>
            </div>
            <div className="relative min-h-0 flex-1 overflow-y-auto">
              <Pre className="text-[13px]">{chunk.text}</Pre>
              <div className="absolute top-1 right-1">
                <CopyButton text={chunk.text} />
              </div>
            </div>
            {certain.length ? (
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                <span className="text-muted-foreground">提到了</span>
                {certain.slice(0, 16).map((entity, index) => (
                  <Badge key={`${entity.name}-${index}`} tone="muted">
                    {entity.name}
                    {entity.type ? <span className="opacity-60">{entity.type}</span> : null}
                  </Badge>
                ))}
                {certain.length > 16 ? (
                  <span className="text-muted-foreground">等 {certain.length} 个</span>
                ) : null}
              </div>
            ) : null}
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <div className="text-muted-foreground flex items-center gap-2">
                <code className="font-mono">{chunk.chunk_id}</code>
                {chunk.url ? (
                  <a
                    href={chunk.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-brand inline-flex items-center gap-1 hover:underline"
                  >
                    原页面 <ExternalLink className="size-3" />
                  </a>
                ) : null}
              </div>
              {chunk.siblings.length > 1 ? (
                <div className="flex items-center gap-1.5">
                  <span className="text-muted-foreground">
                    这篇文档的第 {position + 1} / {chunk.siblings.length} 片
                  </span>
                  <Button
                    variant="outline"
                    size="icon-xs"
                    disabled={!previous}
                    onClick={() => previous && setCurrent(previous.chunk_id)}
                  >
                    <ChevronLeft />
                  </Button>
                  <Button
                    variant="outline"
                    size="icon-xs"
                    disabled={!next}
                    onClick={() => next && setCurrent(next.chunk_id)}
                  >
                    <ChevronRight />
                  </Button>
                </div>
              ) : null}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
