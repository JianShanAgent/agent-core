// 知识库 · 试检索：用和机器人一样的检索查一次，看一个问题会捞到哪几片、各是哪一路找到的。

import { useMutation } from "@tanstack/react-query";
import { ChevronDown, ExternalLink, History, Search, X } from "lucide-react";
import { useState } from "react";

import { ChunkDialog } from "@/components/ChunkDialog";
import { EmptyState, ErrorState, LoadingRows } from "@/components/common";
import { Banner } from "@/components/layout/StateBanners";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/form";
import { Hint } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatNumber, formatSeconds } from "@/lib/format";
import { useStored } from "@/lib/hooks";
import { KB_TYPE_LABELS, label, MODE_LABELS } from "@/lib/labels";
import type { KbSearchHit, KbSearchResult } from "@/lib/types";
import { cn } from "@/lib/utils";

import { ChunkDate, useWikiNames, WikiBadge } from "./shared";

const RECENT_KEY = "jianshanagent.kb.recent-queries";
const RECENT_MAX = 8;

function HitCard({
  hit,
  number,
  hybrid,
  onOpen,
}: {
  hit: KbSearchHit;
  number: number;
  /** 这次两路都用上了：这时「只有关键词找到它」才值得标出来 */
  hybrid: boolean;
  onOpen: () => void;
}) {
  const [open, setOpen] = useState(false);
  const wikiNames = useWikiNames();
  const keywordOnly = hybrid && hit.vector_rank === null && hit.keyword_rank !== null;
  return (
    <div className={cn("px-4 py-3", keywordOnly && "bg-warning/8")}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="bg-muted rounded px-1.5 font-mono text-xs tabular-nums">{number}</span>
        <button
          type="button"
          onClick={onOpen}
          className="hover:text-brand cursor-pointer text-left text-sm font-medium hover:underline"
        >
          {hit.title}
        </button>
        <Badge tone="muted">{label(KB_TYPE_LABELS, hit.type)}</Badge>
        <WikiBadge wiki={hit.wiki} names={wikiNames} />
        {hit.vector_rank !== null ? <Badge tone="info">向量第 {hit.vector_rank} 名</Badge> : null}
        {hit.keyword_rank !== null ? (
          <Badge tone="outline">关键词第 {hit.keyword_rank} 名</Badge>
        ) : null}
        {keywordOnly ? (
          <Hint text="向量那一路的前几名里没有它，是关键词保底的名额把它带进来的。">
            <Badge tone="warning">只靠关键词补进来的</Badge>
          </Hint>
        ) : null}
        {hit.url ? (
          <a
            href={hit.url}
            target="_blank"
            rel="noreferrer noopener"
            className="text-muted-foreground hover:text-foreground ml-auto p-1"
          >
            <ExternalLink className="size-3.5" />
            <span className="sr-only">原页面</span>
          </a>
        ) : null}
      </div>
      <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-2 text-xs">
        {hit.section_path.length ? <span>{hit.section_path.join(" › ")}</span> : null}
        {hit.date || hit.date_inferred ? (
          <span>
            日期 <ChunkDate date={hit.date} inferred={hit.date_inferred} />
          </span>
        ) : null}
        <span>{formatNumber(hit.chars)} 字</span>
      </div>
      <div
        className={cn(
          "mt-1.5 text-[13px] leading-relaxed break-words whitespace-pre-wrap",
          open ? "bg-muted/50 rounded-md border px-3 py-2" : "text-foreground/80 line-clamp-3",
        )}
      >
        {hit.text}
      </div>
      <Button variant="link" size="xs" className="mt-0.5 px-0" onClick={() => setOpen(!open)}>
        {open ? "收起" : "展开全文"}
        <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
      </Button>
    </div>
  );
}

function Result({ result, onOpen }: { result: KbSearchResult; onOpen: (id: string) => void }) {
  const hybrid = result.mode === "hybrid";
  const keywordOnly = result.hits.filter(
    (hit) => hit.vector_rank === null && hit.keyword_rank !== null,
  ).length;
  return (
    <>
      {result.vector_skipped ? (
        <Banner tone="warning" className="mb-0">
          <b>向量这一路没用上，这次只按关键词查的。</b>原因：{result.vector_skipped}
        </Banner>
      ) : null}
      <Card>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-sm">
          <span className="font-medium">「{result.query}」</span>
          <span className="text-muted-foreground text-xs">
            用了{label(MODE_LABELS, result.mode)} · 用时 {formatSeconds(result.seconds)} · 捞到{" "}
            {result.hits.length} 片
            {hybrid && keywordOnly ? `，其中 ${keywordOnly} 片只有关键词找到` : ""}
          </span>
        </div>
        {result.hits.length ? (
          <div className="divide-y border-t">
            {result.hits.map((hit, index) => (
              <HitCard
                key={hit.chunk_id}
                hit={hit}
                number={index + 1}
                hybrid={hybrid}
                onOpen={() => onOpen(hit.chunk_id)}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            className="border-t"
            title="什么都没查到"
            hint="向量和关键词两路都没有找到沾边的切片。换个说法再试试。"
          />
        )}
      </Card>
    </>
  );
}

export function SearchTry() {
  const [text, setText] = useState("");
  const [stored, setRecent] = useStored<string[]>(RECENT_KEY, []);
  const [chunk, setChunk] = useState<string | null>(null);
  const search = useMutation({ mutationFn: (query: string) => api.kbSearch(query) });
  // 浏览器里存的东西可能被改坏：不是一组字符串就当没有
  const recent = Array.isArray(stored) ? stored.filter((item) => typeof item === "string") : [];

  const run = (raw: string) => {
    const query = raw.trim();
    if (!query) return;
    setText(query);
    setRecent([query, ...recent.filter((item) => item !== query)].slice(0, RECENT_MAX));
    search.mutate(query);
  };

  return (
    <div className="flex flex-col gap-3">
      <Card className="p-3">
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            run(text);
          }}
        >
          <div className="relative min-w-0 flex-1">
            <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
            <Input
              autoFocus
              className="pl-9"
              placeholder="像用户那样问一句，比如：须佐之男的技能是什么"
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          </div>
          <Button type="submit" loading={search.isPending} disabled={!text.trim()}>
            检索
          </Button>
        </form>
        <div className="text-muted-foreground mt-2 text-xs leading-relaxed">
          用和机器人一样的检索查一次，看一个问题会捞到哪几片。机器人查证时还会先把问题改写成检索用的问句，这里是你写什么就查什么。
        </div>
        {recent.length ? (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-muted-foreground flex items-center gap-1 text-xs">
              <History className="size-3.5" /> 最近查过
            </span>
            {recent.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => run(item)}
                className="bg-muted hover:bg-accent text-foreground/80 max-w-64 cursor-pointer truncate rounded-md px-2 py-0.5 text-xs transition-colors"
              >
                {item}
              </button>
            ))}
            <Hint text="清掉最近查过的">
              <Button
                variant="ghost"
                size="icon-xs"
                className="text-muted-foreground size-6"
                onClick={() => setRecent([])}
              >
                <X className="size-3.5" />
                <span className="sr-only">清掉最近查过的</span>
              </Button>
            </Hint>
          </div>
        ) : null}
      </Card>

      {search.isPending ? (
        <Card>
          <LoadingRows rows={6} />
        </Card>
      ) : search.isError ? (
        <Card>
          <ErrorState error={search.error} onRetry={() => run(search.variables ?? text)} />
        </Card>
      ) : search.data ? (
        <Result result={search.data} onOpen={setChunk} />
      ) : (
        <Card>
          <EmptyState
            icon={Search}
            title="输入一个问题，看知识库会给出哪几片"
            hint="每一片会标出它在向量、关键词两路里各排第几。回答不对的时候来这里查：是资料没捞到，还是捞到了模型没用好。"
          />
        </Card>
      )}
      <ChunkDialog chunkId={chunk} onClose={() => setChunk(null)} />
    </div>
  );
}
