// 知识库 · 概况：库里有多少东西、向量能不能用、检索是按什么参数取的。

import { ArrowRight, DatabaseZap, Images, Layers, Ruler, Waypoints } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { EmptyState, ErrorState, LoadingRows, StatCard } from "@/components/common";
import { Banner } from "@/components/layout/StateBanners";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton, Table, Td, Th, Tr } from "@/components/ui/misc";
import { formatAgo, formatDateTime, formatNumber } from "@/lib/format";
import { KB_TYPE_LABELS } from "@/lib/labels";
import type { KbSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

import { useKbSummary } from "./shared";
import { SyncStatus } from "./SyncStatus";

const CONFIG_LINK = "/config/plugin.knowledge";
const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);

/** 向量现在用不了的原因，说成人话。 */
function vectorProblem(store: NonNullable<KbSummary["store"]>): ReactNode {
  if (store.query_fingerprint === null) {
    return (
      <>
        <b>没有配置嵌入接口，问题的向量算不出来。</b>
        现在检索只能用关键词一路，问法和原文用词不一样的就找不到了。嵌入接口在「机器人配置」的
        core.embedding 里配。
      </>
    );
  }
  if (store.fingerprint === null) {
    return (
      <>
        <b>库里的切片还没有向量。</b>
        现在检索只能用关键词一路。要先给切片算向量、再导入一次（命令行：python -m
        jianshanagent.plugins.knowledge embed，然后 load）。
      </>
    );
  }
  return (
    <>
      <b>库里向量的指纹和现在配置的嵌入接口对不上，这些向量不能用，检索只能用关键词一路。</b>
      <div className="mt-0.5 text-xs">
        库里的是 <code className="font-mono">{store.fingerprint}</code>，现在配置的是{" "}
        <code className="font-mono">{store.query_fingerprint}</code>
        。换过嵌入接口或型号的话，要么改回去，要么重新算向量再导入。
      </div>
    </>
  );
}

/** 一个参数：现在的值、它叫什么、一句话解释。 */
function Param({
  value,
  name,
  code,
  children,
}: {
  value: ReactNode;
  name: string;
  code: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-3 py-2">
      <div className="w-12 shrink-0 text-right text-lg font-semibold tabular-nums">{value}</div>
      <div className="min-w-0">
        <div className="text-sm font-medium">
          {name}
          <code className="text-muted-foreground ml-2 font-mono text-[11px] font-normal">{code}</code>
        </div>
        <div className="text-muted-foreground mt-0.5 text-xs leading-relaxed">{children}</div>
      </div>
    </div>
  );
}

function Tables({ data }: { data: KbSummary }) {
  const { types, image_kinds: kinds, images } = data;
  return (
    <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>切片按类型分</CardTitle>
            <CardDescription>一篇文档（一个 wiki 页面、一条公告）按章节切成若干片</CardDescription>
          </div>
        </CardHeader>
        {types.length ? (
          <Table>
            <thead>
              <tr>
                <Th>类型</Th>
                <Th className="text-right">文档</Th>
                <Th className="text-right">切片</Th>
                <Th className="text-right">有向量的</Th>
                <Th className="text-right">总字数</Th>
              </tr>
            </thead>
            <tbody>
              {types.map((row) => (
                <Tr key={row.type}>
                  <Td className="font-medium">
                    {KB_TYPE_LABELS[row.type] ?? data.type_names[row.type] ?? row.type}
                  </Td>
                  <Td className="text-right tabular-nums">{formatNumber(row.docs)}</Td>
                  <Td className="text-right tabular-nums">{formatNumber(row.chunks)}</Td>
                  <Td
                    className={cn(
                      "text-right tabular-nums",
                      row.with_vectors < row.chunks && "text-warning",
                    )}
                  >
                    {formatNumber(row.with_vectors)}
                  </Td>
                  <Td className="text-muted-foreground text-right tabular-nums">
                    {formatNumber(row.chars)}
                  </Td>
                </Tr>
              ))}
              {types.length > 1 ? (
                <Tr className="font-medium">
                  <Td>合计</Td>
                  <Td className="text-right tabular-nums">
                    {formatNumber(sum(types.map((row) => row.docs)))}
                  </Td>
                  <Td className="text-right tabular-nums">
                    {formatNumber(sum(types.map((row) => row.chunks)))}
                  </Td>
                  <Td className="text-right tabular-nums">
                    {formatNumber(sum(types.map((row) => row.with_vectors)))}
                  </Td>
                  <Td className="text-muted-foreground text-right tabular-nums">
                    {formatNumber(sum(types.map((row) => row.chars)))}
                  </Td>
                </Tr>
              ) : null}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            className="py-8"
            title="库里还没有切片"
            hint="切片是用命令行导入的（python -m jianshanagent.plugins.knowledge）：先 embed 算向量，再 load 导入数据库。"
          />
        )}
      </Card>
      <Card>
        <CardHeader>
          <div>
            <CardTitle>图库按种类分</CardTitle>
            <CardDescription>
              用户发来图片时拿来比对的 wiki 图片
              {images?.fingerprint ? (
                <>
                  {" "}
                  · 向量指纹 <code className="font-mono">{images.fingerprint}</code>
                </>
              ) : null}
              {images?.loaded_at ? ` · 上次导入 ${formatDateTime(images.loaded_at)}` : ""}
            </CardDescription>
          </div>
        </CardHeader>
        {kinds.length ? (
          <Table>
            <thead>
              <tr>
                <Th>种类</Th>
                <Th className="text-right">张数</Th>
                <Th className="text-right">有向量的</Th>
              </tr>
            </thead>
            <tbody>
              {kinds.map((row) => (
                <Tr key={row.kind}>
                  <Td className="font-medium">{row.kind}</Td>
                  <Td className="text-right tabular-nums">{formatNumber(row.images)}</Td>
                  <Td
                    className={cn(
                      "text-right tabular-nums",
                      row.with_vectors < row.images && "text-warning",
                    )}
                  >
                    {formatNumber(row.with_vectors)}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            className="py-8"
            title="图库是空的"
            hint="图片也是用命令行导入的（python -m jianshanagent.plugins.knowledge images collect / embed / load）。"
          />
        )}
      </Card>
    </div>
  );
}

/** 挂了不止一个 wiki 时：库里各有多少。 */
function Wikis({ wikis }: { wikis: KbSummary["wikis"] }) {
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>内容出自这几个 wiki</CardTitle>
          <CardDescription>
            它们的内容在同一个库里，检索时一起找，回答里标明出自哪一个；各更新各的
          </CardDescription>
        </div>
      </CardHeader>
      <Table>
        <thead>
          <tr>
            <Th>wiki</Th>
            <Th className="text-right">切片</Th>
            <Th className="text-right">有向量的</Th>
            <Th className="text-right">图片</Th>
            <Th>上次导入</Th>
          </tr>
        </thead>
        <tbody>
          {wikis.map((wiki) => (
            <Tr key={wiki.id}>
              <Td className="font-medium">
                {wiki.label}
                <code className="text-muted-foreground ml-2 font-mono text-[11px] font-normal">
                  {wiki.id}
                </code>
              </Td>
              <Td className="text-right tabular-nums">{formatNumber(wiki.chunks)}</Td>
              <Td
                className={cn(
                  "text-right tabular-nums",
                  wiki.with_vectors < wiki.chunks && "text-warning",
                )}
              >
                {formatNumber(wiki.with_vectors)}
              </Td>
              <Td className="text-right tabular-nums">{formatNumber(wiki.images)}</Td>
              <Td className="text-muted-foreground text-xs">
                {wiki.loaded_at
                  ? `${formatDateTime(wiki.loaded_at)}（${formatAgo(wiki.loaded_at)}）`
                  : "还没导入过"}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

export function Overview({ onOpenTab }: { onOpenTab: (tab: "chunks" | "images") => void }) {
  const query = useKbSummary();
  const data = query.data;

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {Array.from({ length: 4 }, (_, index) => (
            <Skeleton key={index} className="h-24" />
          ))}
        </div>
        <Card>
          <LoadingRows />
        </Card>
      </div>
    );
  }
  if (query.isError || !data) {
    return (
      <Card>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    );
  }

  const hasRows = data.types.length > 0 || data.image_kinds.length > 0;
  if (!data.available) {
    return (
      <div className="flex flex-col gap-3">
        <Card>
          <EmptyState
            icon={DatabaseZap}
            title="知识库检索没有启用"
            hint={
              <>
                没有配置数据库，或者配置里把检索关掉了（plugin.knowledge 的
                retrieval.enabled）。机器人现在查证时用不上知识库，「试检索」也用不了。
                {hasRows ? "数据库里以前导入的切片还在，下面能看到。" : ""}
              </>
            }
            action={
              <Button asChild variant="outline" size="sm" className="mt-1">
                <Link to={CONFIG_LINK}>去看知识库的配置</Link>
              </Button>
            }
          />
        </Card>
        {hasRows ? <Tables data={data} /> : null}
      </div>
    );
  }

  const { store, images, settings } = data;
  const chunks = store?.chunks ?? sum(data.types.map((row) => row.chunks));
  const withVectors = store?.with_vectors ?? sum(data.types.map((row) => row.with_vectors));
  const chars = sum(data.types.map((row) => row.chars));
  const average = store?.average_chars ?? (chunks ? chars / chunks : 0);
  const imageCount = images?.images ?? sum(data.image_kinds.map((row) => row.images));
  const imageVectors = images?.with_vectors ?? sum(data.image_kinds.map((row) => row.with_vectors));

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="切片"
          value={formatNumber(chunks)}
          icon={Layers}
          hint={`来自 ${formatNumber(sum(data.types.map((row) => row.docs)))} 篇文档`}
          onClick={() => onOpenTab("chunks")}
        />
        <StatCard
          label="其中有向量的"
          value={formatNumber(withVectors)}
          icon={Waypoints}
          tone={withVectors < chunks ? "warning" : undefined}
          hint={
            withVectors < chunks
              ? `${formatNumber(chunks - withVectors)} 片没有，只能靠关键词找到`
              : "每一片都有"
          }
        />
        <StatCard
          label="平均每片"
          value={`${formatNumber(average)} 字`}
          icon={Ruler}
          hint={`一共 ${formatNumber(chars)} 字`}
        />
        <StatCard
          label="图库"
          value={`${formatNumber(imageCount)} 张`}
          icon={Images}
          tone={imageVectors < imageCount ? "warning" : undefined}
          hint={
            imageVectors < imageCount
              ? `${formatNumber(imageCount - imageVectors)} 张没有向量，以图搜图比不到`
              : "以图搜图用，每张都有向量"
          }
          onClick={() => onOpenTab("images")}
        />
      </div>

      {store ? (
        <div className="text-muted-foreground flex flex-wrap items-center gap-x-5 gap-y-1 px-1 text-xs">
          <span>
            向量指纹（算这些向量用的嵌入接口和型号）
            <code className="text-foreground/80 ml-1.5 font-mono">{store.fingerprint ?? "没有"}</code>
          </span>
          <span>
            上次导入{" "}
            {store.loaded_at
              ? `${formatDateTime(store.loaded_at)}（${formatAgo(store.loaded_at)}）`
              : "还没导入过"}
          </span>
        </div>
      ) : null}

      {store && !store.vectors_usable ? (
        <Banner tone="warning" className="mb-0">
          {vectorProblem(store)}
        </Banner>
      ) : null}

      {data.wikis.length > 1 ? <Wikis wikis={data.wikis} /> : null}
      {data.wikis.length > 1 ? (
        data.wikis.map((wiki) => <SyncStatus key={wiki.id} sync={wiki.sync} wiki={wiki} />)
      ) : (
        <SyncStatus sync={data.wikis[0]?.sync ?? null} />
      )}

      <Tables data={data} />

      <Card>
        <CardHeader>
          <div>
            <CardTitle>检索是怎么取的</CardTitle>
            <CardDescription>
              向量为主、关键词补位：问题先算成向量找意思最近的切片，再用问题里的字眼补上向量漏掉的
            </CardDescription>
          </div>
          <Button asChild variant="ghost" size="xs">
            <Link to={CONFIG_LINK}>
              去改这些参数 <ArrowRight />
            </Link>
          </Button>
        </CardHeader>
        <CardContent className="grid gap-x-8 md:grid-cols-2">
          <div>
            <div className="text-muted-foreground text-xs">查切片</div>
            {settings ? (
              <>
                <Param value={settings.top_k} name="一次取几片" code="retrieval.top_k">
                  每查一次知识库，交给查证员 {settings.top_k} 片。
                </Param>
                <Param
                  value={settings.keyword_slots}
                  name="关键词保底的名额"
                  code="retrieval.keyword_slots"
                >
                  {settings.keyword_slots > 0
                    ? `关键词检索的前 ${settings.keyword_slots} 名一定给（向量找不到的少见字眼靠它），其余 ${Math.max(0, settings.top_k - settings.keyword_slots)} 个名额按向量的名次补满。`
                    : "现在是 0：只用向量，关键词不保底。"}
                </Param>
              </>
            ) : (
              <div className="text-muted-foreground py-2 text-sm">检索没有启用。</div>
            )}
          </div>
          <div>
            <div className="text-muted-foreground text-xs">以图搜图</div>
            {images ? (
              <>
                <Param value={images.min_score.toFixed(2)} name="最低相似度" code="images.min_score">
                  和图库里的图相似度低于它的不算对上，不返回。
                </Param>
                <Param value={images.sure_score.toFixed(2)} name="认定的门槛" code="images.sure_score">
                  最像的那张，相似度至少到这个数，才可能认定「就是这张图」。
                </Param>
                <Param
                  value={images.sure_margin.toFixed(2)}
                  name="要领先第二名多少"
                  code="images.sure_margin"
                >
                  还得比第二像的高出这么多才认定（相似度越接近 1，要求领先得越少）。前两名挤在一起时只说「有些像」。
                </Param>
              </>
            ) : (
              <div className="text-muted-foreground py-2 text-sm">
                以图搜图没有启用（要有数据库和嵌入接口）。
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
