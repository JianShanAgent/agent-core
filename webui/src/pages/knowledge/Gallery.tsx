// 知识库 · 图库：翻以图搜图用的那批 wiki 图片。

import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { EmptyState, ErrorState, Pager } from "@/components/common";
import { Card } from "@/components/ui/card";
import { Skeleton, Spinner } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { useDebounced } from "@/lib/hooks";
import type { KbImage } from "@/lib/types";
import { SearchBox, useOffset } from "@/pages/sessions/shared";

import { ImageDialog, KbImg, useKbSummary, useWikiNames, wikiOptions } from "./shared";

const PAGE = 48;
const GRID = "grid grid-cols-2 gap-3 p-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6";

export function Gallery() {
  const [text, setText] = useState("");
  const [kind, setKind] = useState("");
  const [wiki, setWiki] = useState("");
  const [image, setImage] = useState<KbImage | null>(null);
  const top = useRef<HTMLDivElement>(null);
  const q = useDebounced(text.trim());
  const [offset, setOffset] = useOffset(`${kind}\n${wiki}\n${q}`);

  const summary = useKbSummary();
  const wikiNames = useWikiNames();
  const query = useQuery({
    queryKey: ["kb", "images", { q, kind, wiki, offset }],
    queryFn: () =>
      api.kbImages({
        q: q || undefined,
        kind: kind || undefined,
        wiki: wiki || undefined,
        limit: PAGE,
        offset,
      }),
    placeholderData: (previous) => previous,
  });
  const data = query.data;
  const filtered = q !== "" || kind !== "" || wiki !== "";
  const kinds = (summary.data?.image_kinds ?? []).map((row) => ({
    value: row.kind,
    label: `${row.kind}（${formatNumber(row.images)}）`,
  }));

  return (
    <Card ref={top} className="scroll-mt-4">
      <div className="flex flex-wrap items-center gap-2 p-3">
        <SearchBox
          className="min-w-52 flex-1"
          placeholder="搜图片的名字、所属页面、文件名"
          value={text}
          onChange={setText}
        />
        <SimpleSelect
          size="sm"
          value={kind}
          onChange={setKind}
          options={[{ value: "", label: "全部种类" }, ...kinds]}
        />
        {wikiNames ? (
          <SimpleSelect size="sm" value={wiki} onChange={setWiki} options={wikiOptions(wikiNames)} />
        ) : null}
        <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
          {query.isFetching && data ? <Spinner className="size-3.5" /> : null}
          {data ? `${filtered ? "找到" : "共"} ${formatNumber(data.total)} 张` : ""}
        </span>
      </div>
      <div className="border-t">
        {query.isPending ? (
          <div className={GRID}>
            {Array.from({ length: 12 }, (_, index) => (
              <Skeleton key={index} className="aspect-square" />
            ))}
          </div>
        ) : query.isError || !data ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : data.items.length === 0 ? (
          <EmptyState
            title={filtered ? "没有符合条件的图片" : "图库是空的"}
            hint={
              filtered
                ? "换个词，或者把种类放宽。"
                : "图库是用命令行导入的（python -m jianshanagent.plugins.knowledge images collect / embed / load）。里面有图，以图搜图才有东西可比。"
            }
          />
        ) : (
          <>
            <div className={GRID}>
              {data.items.map((item) => (
                <button
                  key={item.image_id}
                  type="button"
                  onClick={() => setImage(item)}
                  className="hover:border-foreground/30 flex min-w-0 cursor-pointer flex-col overflow-hidden rounded-lg border text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                >
                  <div className="bg-muted aspect-square w-full overflow-hidden">
                    <KbImg key={item.image_url} src={item.image_url} alt={item.label} />
                  </div>
                  <div className="w-full px-2 py-1.5">
                    <div className="truncate text-xs font-medium">{item.label || item.image_id}</div>
                    <div className="text-muted-foreground truncate text-[11px]">
                      {item.kind}
                      {wikiNames && item.wiki ? ` · ${wikiNames[item.wiki] ?? item.wiki}` : ""}
                      {item.has_vector ? "" : " · 没有向量"}
                    </div>
                  </div>
                </button>
              ))}
            </div>
            <div className="border-t empty:hidden">
              <Pager
                offset={offset}
                limit={PAGE}
                total={data.total}
                onChange={(next) => {
                  setOffset(next);
                  // 翻页的按钮在最底下：翻完回到第一排
                  top.current?.scrollIntoView({ block: "start" });
                }}
              />
            </div>
          </>
        )}
      </div>
      <ImageDialog image={image} onClose={() => setImage(null)} />
    </Card>
  );
}
