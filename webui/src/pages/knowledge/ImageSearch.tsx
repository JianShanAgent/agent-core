// 知识库 · 以图搜图：传一张图，用和机器人一样的办法在图库里找，看它会认成哪张。

import { useMutation } from "@tanstack/react-query";
import { ImageUp, ScanSearch } from "lucide-react";
import { type DragEvent, useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, LoadingRows } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatBytes, formatSeconds } from "@/lib/format";
import type { ImageSearchHit } from "@/lib/types";
import { cn } from "@/lib/utils";

import { ImageDialog, KbImg, useKbSummary } from "./shared";

// 和后端的上限一致（routes_data.py）
const MAX_BYTES = 10 * 1024 * 1024;
// 右边那一栏的宽度随窗口变得厉害：按能放下几张来排，不按窗口宽度定列数
const GRID = "grid grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-3";

interface Picked {
  uri: string;
  name: string;
  size: number;
}

function SureBadge({ sure }: { sure: boolean }) {
  return sure ? (
    <Badge tone="success">认定就是这张图</Badge>
  ) : (
    <Badge tone="warning">有些像，不能确定</Badge>
  );
}

export function ImageSearch({ active }: { active: boolean }) {
  const summary = useKbSummary();
  const [picked, setPicked] = useState<Picked | null>(null);
  const [dragging, setDragging] = useState(false);
  const [opened, setOpened] = useState<ImageSearchHit | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const search = useMutation({ mutationFn: (image: string) => api.kbImageSearch(image) });
  const { mutate } = search;
  const enabled = summary.data?.image_search === true;

  const take = useCallback(
    (file: File) => {
      if (!file.type.startsWith("image/")) {
        toast.error("这不是图片", { description: file.name });
        return;
      }
      if (file.size > MAX_BYTES) {
        toast.error(`这张图片太大了（${formatBytes(file.size)}），最多 10 MB`);
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const uri = typeof reader.result === "string" ? reader.result : "";
        if (!uri) return;
        setPicked({ uri, name: file.name, size: file.size });
        mutate(uri);
      };
      reader.onerror = () => toast.error("这张图片读不出来");
      reader.readAsDataURL(file);
    },
    [mutate],
  );

  // 直接粘贴截图：只在这个标签开着的时候听，而且只拿图片（粘贴文字不管）
  useEffect(() => {
    if (!active || !enabled) return;
    const onPaste = (event: ClipboardEvent) => {
      const file = Array.from(event.clipboardData?.files ?? []).find((item) =>
        item.type.startsWith("image/"),
      );
      if (!file) return;
      event.preventDefault();
      take(file);
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [active, enabled, take]);

  if (summary.isPending) {
    return (
      <Card>
        <LoadingRows />
      </Card>
    );
  }
  if (summary.isError) {
    return (
      <Card>
        <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />
      </Card>
    );
  }
  if (!enabled) {
    return (
      <Card>
        <EmptyState
          icon={ScanSearch}
          title="以图搜图没有启用"
          hint="要有数据库和能算图片向量的嵌入接口，并且知识库检索是开着的（plugin.knowledge 的 retrieval.enabled）。现在用户发图片来，机器人认不出是哪张。"
        />
      </Card>
    );
  }

  const hits = search.data?.hits ?? [];

  return (
    // 整个标签都接拖进来的图：拖偏了落在别处的话，浏览器会直接把图片打开、把页面顶掉
    <div
      className="grid items-start gap-3 md:grid-cols-[18rem_minmax(0,1fr)]"
      onDragOver={(event: DragEvent) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(event: DragEvent) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={(event: DragEvent) => {
        event.preventDefault();
        setDragging(false);
        const file = event.dataTransfer.files[0];
        if (file) take(file);
      }}
    >
      <Card className="p-3">
        <input
          ref={input}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) take(file);
            // 清掉，不然再选同一张不会触发
            event.target.value = "";
          }}
        />
        <div
          className={cn(
            "flex flex-col items-center gap-2 rounded-lg border border-dashed p-3 text-center transition-colors",
            dragging && "border-brand bg-brand/8",
          )}
        >
          {picked ? (
            <div className="bg-muted flex aspect-square w-full items-center justify-center overflow-hidden rounded-md">
              <img src={picked.uri} alt="要查的图片" className="size-full object-contain" />
            </div>
          ) : (
            <ImageUp className="text-muted-foreground mt-4 size-8 opacity-50" />
          )}
          {picked ? (
            <div className="text-muted-foreground w-full truncate text-xs">
              {picked.name || "粘贴的图片"} · {formatBytes(picked.size)}
            </div>
          ) : null}
          <Button variant="outline" size="sm" onClick={() => input.current?.click()}>
            <ImageUp /> {picked ? "换一张" : "选一张图片"}
          </Button>
          <div className={cn("text-muted-foreground text-xs leading-relaxed", !picked && "mb-3")}>
            也可以把图片拖进来，或者直接粘贴（Ctrl / ⌘ + V）。单张最多 10 MB。
          </div>
        </div>
        <div className="text-muted-foreground mt-2.5 text-xs leading-relaxed">
          用和机器人一样的办法在图库里找，截图、带界面的图也可以试。图片只用来查这一次，不存。
        </div>
      </Card>

      <Card className="min-w-0">
        {search.isPending ? (
          <div className="flex flex-col gap-3 p-4">
            <div className="text-muted-foreground text-xs">正在图库里找……</div>
            <div className={GRID}>
              <Skeleton className="h-64" />
            </div>
          </div>
        ) : search.isError ? (
          <ErrorState
            error={search.error}
            onRetry={picked ? () => search.mutate(picked.uri) : undefined}
          />
        ) : !search.data ? (
          <EmptyState
            icon={ScanSearch}
            title="放一张图片进来，看图库里哪张最像"
            hint="认定了的，机器人只拿那一张当资料；没认定的，最像的几张都标着「有些像」交给模型。"
          />
        ) : hits.length === 0 ? (
          <EmptyState
            icon={ScanSearch}
            title="图库里没有对得上的图"
            hint={`用时 ${formatSeconds(search.data.seconds)}。最像的那张相似度也没到门槛（「概况」里的最低相似度）。图库里只有站点插件收进来的那几类图（阴阳师 wiki 是立绘、头像、御魂图标），别的认不出来。`}
          />
        ) : (
          <>
            <div className="text-muted-foreground px-4 pt-3 text-xs">
              用时 {formatSeconds(search.data.seconds)} ·{" "}
              {hits[0].sure
                ? "认准了，所以只给这一张"
                : `没有哪一张能认准，给出最像的 ${hits.length} 张`}
            </div>
            <div className={cn(GRID, "p-4")}>
              {hits.map((hit) => (
                <button
                  key={hit.image_id}
                  type="button"
                  onClick={() => setOpened(hit)}
                  className={cn(
                    "hover:border-foreground/30 flex min-w-0 cursor-pointer flex-col overflow-hidden rounded-lg border text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                    hit.sure && "border-success/50",
                  )}
                >
                  <div className="bg-muted aspect-square w-full overflow-hidden">
                    <KbImg key={hit.image_url} src={hit.image_url} alt={hit.label} lazy={false} />
                  </div>
                  <div className="flex w-full flex-col gap-1.5 p-2.5">
                    <div className="text-sm font-medium break-words">{hit.label || hit.image_id}</div>
                    <div className="text-muted-foreground flex items-center justify-between gap-2 text-xs">
                      <span className="truncate">{hit.kind}</span>
                      <span className="text-foreground shrink-0 font-medium tabular-nums">
                        相似度 {hit.score.toFixed(2)}
                      </span>
                    </div>
                    <SureBadge sure={hit.sure} />
                  </div>
                </button>
              ))}
            </div>
          </>
        )}
      </Card>
      <ImageDialog
        image={opened}
        onClose={() => setOpened(null)}
        extra={
          opened ? (
            <>
              <Badge tone="muted">相似度 {opened.score.toFixed(2)}</Badge>
              <SureBadge sure={opened.sure} />
            </>
          ) : null
        }
      />
    </div>
  );
}
