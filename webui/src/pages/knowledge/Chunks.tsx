// 知识库 · 切片：翻库里的切片，按标题和正文搜，点一行看全文。

import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { ChunkDialog } from "@/components/ChunkDialog";
import { EmptyState, ErrorState, LoadingRows, Pager } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Spinner, Table, Td, Th, Tr } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { useDebounced } from "@/lib/hooks";
import { KB_TYPE_LABELS, label } from "@/lib/labels";
import { SearchBox, useOffset } from "@/pages/sessions/shared";

import { ChunkDate, useKbSummary } from "./shared";

const PAGE = 30;

export function Chunks() {
  const [text, setText] = useState("");
  const [type, setType] = useState("");
  const [chunk, setChunk] = useState<string | null>(null);
  const top = useRef<HTMLDivElement>(null);
  const q = useDebounced(text.trim());
  const [offset, setOffset] = useOffset(`${type}\n${q}`);

  const summary = useKbSummary();
  const query = useQuery({
    queryKey: ["kb", "chunks", { q, type, offset }],
    queryFn: () =>
      api.kbChunks({ q: q || undefined, type: type || undefined, limit: PAGE, offset }),
    placeholderData: (previous) => previous,
  });
  const data = query.data;
  const filtered = q !== "" || type !== "";

  const types = summary.data?.types.length
    ? summary.data.types.map((row) => ({
        value: row.type,
        label: `${label(KB_TYPE_LABELS, row.type)}（${formatNumber(row.chunks)}）`,
      }))
    : Object.entries(KB_TYPE_LABELS).map(([value, name]) => ({ value, label: name }));

  return (
    <Card ref={top} className="scroll-mt-4">
      <div className="flex flex-wrap items-center gap-2 p-3">
        <SearchBox
          className="min-w-52 flex-1"
          placeholder="搜标题和正文里的字"
          value={text}
          onChange={setText}
        />
        <SimpleSelect
          size="sm"
          value={type}
          onChange={setType}
          options={[{ value: "", label: "全部类型" }, ...types]}
        />
        <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
          {query.isFetching && data ? <Spinner className="size-3.5" /> : null}
          {data ? `${filtered ? "找到" : "共"} ${formatNumber(data.total)} 片` : ""}
        </span>
      </div>
      <div className="border-t">
        {query.isPending ? (
          <LoadingRows rows={8} />
        ) : query.isError || !data ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : data.items.length === 0 ? (
          <EmptyState
            title={filtered ? "没有符合条件的切片" : "知识库里还没有切片"}
            hint={
              filtered
                ? "这里是按字面搜标题和正文的（不是机器人用的那种检索）。换个词，或者去「试检索」用问句查。"
                : "切片是用命令行导入的（python -m jianshanagent.plugins.knowledge）：先 embed 给切片算向量，再 load 导入数据库。"
            }
          />
        ) : (
          <>
            <Table className="min-w-[46rem] table-fixed">
              <thead>
                <tr>
                  <Th className="w-[30%]">标题和章节</Th>
                  <Th className="w-16">类型</Th>
                  <Th className="w-32">日期</Th>
                  <Th className="w-16 text-right">字数</Th>
                  <Th>正文开头</Th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <Tr
                    key={row.chunk_id}
                    className="cursor-pointer"
                    onClick={() => setChunk(row.chunk_id)}
                  >
                    <Td className="align-top">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate font-medium">{row.title}</span>
                        {row.has_vector ? null : <Badge tone="warning">没有向量</Badge>}
                      </div>
                      {row.section_path.length ? (
                        <div className="text-muted-foreground mt-0.5 truncate text-xs">
                          {row.section_path.join(" › ")}
                        </div>
                      ) : null}
                    </Td>
                    <Td className="align-top whitespace-nowrap">
                      <Badge tone="muted">{label(KB_TYPE_LABELS, row.type)}</Badge>
                    </Td>
                    <Td className="align-top text-xs whitespace-nowrap">
                      <ChunkDate date={row.date} inferred={row.date_inferred} />
                    </Td>
                    <Td className="text-muted-foreground text-right align-top text-xs tabular-nums">
                      {formatNumber(row.chars)}
                    </Td>
                    <Td className="align-top">
                      <div className="text-muted-foreground line-clamp-2 text-xs leading-relaxed break-words">
                        {row.preview}
                      </div>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
            <div className="border-t empty:hidden">
              <Pager
                offset={offset}
                limit={PAGE}
                total={data.total}
                onChange={(next) => {
                  setOffset(next);
                  // 翻页的按钮在最底下：翻完回到表格开头
                  top.current?.scrollIntoView({ block: "start" });
                }}
              />
            </div>
          </>
        )}
      </div>
      <ChunkDialog chunkId={chunk} onClose={() => setChunk(null)} />
    </Card>
  );
}
