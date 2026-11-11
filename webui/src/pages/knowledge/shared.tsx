// 知识库页几个标签共用的：概况的查询、切片的日期、图库里的一张图和它的详情对话框。

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, ImageOff } from "lucide-react";
import { type ReactNode, useState } from "react";

import { CopyButton } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/** 知识库概况：几个标签都要用（类型和种类的下拉、以图搜图有没有启用），共用一份缓存。 */
export function useKbSummary() {
  return useQuery({ queryKey: ["kb", "summary"], queryFn: api.kbSummary });
}

/** 切片的日期：页面上写明的用它；没有就用推断出来的，并标一下。 */
export function ChunkDate({
  date,
  inferred,
}: {
  date: string | null;
  inferred: string | null;
}) {
  if (date) return <span className="tabular-nums">{date}</span>;
  if (inferred) {
    return (
      <span className="tabular-nums">
        {inferred}
        <span className="text-muted-foreground ml-1 text-[11px]">推断</span>
      </span>
    );
  }
  return <span className="text-muted-foreground">—</span>;
}

/**
 * 图库里的一张图，铺满外面的格子。图片在 wiki 的图床上，可能加载不出来（外链失效、被拦）：
 * 那时放一个占位图标，不显示破图。换图时调用方给它一个新的 key。
 */
export function KbImg({
  src,
  alt,
  lazy = true,
  className,
}: {
  src: string;
  alt: string;
  lazy?: boolean;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return (
      <div className="text-muted-foreground flex size-full flex-col items-center justify-center gap-1 text-[11px]">
        <ImageOff className="size-6 opacity-50" />
        图片加载不出来
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={alt}
      loading={lazy ? "lazy" : undefined}
      referrerPolicy="no-referrer"
      className={cn("size-full object-contain", className)}
      onError={() => setFailed(true)}
    />
  );
}

/** 图库浏览和以图搜图的结果都有的那几项。 */
export interface ImageInfo {
  image_id: string;
  kind: string;
  label: string;
  page_title: string;
  url: string;
  image_url: string;
  text: string;
}

/** 一张图的详情：大图、说明文字、所属页面、文件名。 */
export function ImageDialog({
  image,
  onClose,
  extra,
}: {
  image: ImageInfo | null;
  onClose: () => void;
  /** 标题旁边多放的标签（以图搜图的相似度） */
  extra?: ReactNode;
}) {
  return (
    <Dialog open={image !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        {image ? (
          <>
            <div className="pr-8">
              <DialogTitle className="flex flex-wrap items-center gap-2">
                {image.label || image.image_id}
                <Badge tone="outline">{image.kind}</Badge>
                {extra}
              </DialogTitle>
              <DialogDescription className="sr-only">图库里的一张图</DialogDescription>
            </div>
            <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
              <div className="bg-muted flex h-[min(52vh,30rem)] shrink-0 items-center justify-center overflow-hidden rounded-lg">
                <KbImg key={image.image_url} src={image.image_url} alt={image.label} lazy={false} />
              </div>
              {image.text ? (
                <div>
                  <div className="text-muted-foreground mb-1 text-xs">
                    说明文字（认出这张图以后，交给模型当资料的就是这段）
                  </div>
                  <div className="text-sm leading-relaxed break-words whitespace-pre-wrap">
                    {image.text}
                  </div>
                </div>
              ) : null}
            </div>
            <div className="text-muted-foreground flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs">
              <span className="inline-flex min-w-0 items-center gap-1">
                文件名 <code className="font-mono break-all">{image.image_id}</code>
                <CopyButton text={image.image_id} label="复制文件名" />
              </span>
              {image.url ? (
                <a
                  href={image.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-brand inline-flex items-center gap-1 hover:underline"
                >
                  所属页面：{image.page_title || "打开"} <ExternalLink className="size-3" />
                </a>
              ) : image.page_title ? (
                <span>所属页面：{image.page_title}</span>
              ) : null}
            </div>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
