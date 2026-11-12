// 知识库 · 概况里的「自动更新」：服务器上的 kbsync 服务上次什么时候看过 wiki、更新了什么、
// 有没有被闸门拦下的事。状态是它每做完一轮写进数据库的（后端 plugins/knowledge/sync.py）。

import { ArrowRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { ToneBadge } from "@/components/common";
import { Banner } from "@/components/layout/StateBanners";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, Td, Th, Tr } from "@/components/ui/misc";
import { formatAgo, formatDateTime, formatNumber, formatSeconds, formatTime, parseTime } from "@/lib/format";
import type { Tone } from "@/lib/labels";
import type { KbSync, KbSyncRound } from "@/lib/types";

const CONFIG_LINK = "/config/plugin.knowledge";

const RESULTS: Record<string, { text: string; tone: Tone }> = {
  unchanged: { text: "没有改动", tone: "muted" },
  checked: { text: "对过了，没变", tone: "muted" },
  updated: { text: "有更新", tone: "success" },
  problem: { text: "有步骤没做成", tone: "warning" },
};

/** 重新渲染一个页面的原因（后端 sources.py 的 RENDER_*）。 */
const RENDERS: Record<string, string> = {
  revision: "有新修订",
  touched: "模板或模块改了",
  data: "Data 改了",
  refresh: "全部重取",
};

const PARTS: Record<string, string> = {
  check: "看 wiki 的最近更改",
  kb: "知识库",
  images: "图库",
};

/** 上次检查过了两个周期再加 5 分钟还没有下一次：服务多半没在跑。 */
function overdue(sync: KbSync): boolean {
  const checked = parseTime(sync.checked_at);
  if (!sync.enabled || !checked) return false;
  return Date.now() - checked.getTime() > (sync.interval_seconds * 2 + 300) * 1000;
}

function Fact({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <div className="text-muted-foreground text-xs">{label}</div>
      <div className="mt-0.5 text-sm font-medium tabular-nums">
        {value ? formatAgo(value) : "还没有过"}
      </div>
      {value ? (
        <div className="text-muted-foreground text-[11px] tabular-nums">{formatDateTime(value)}</div>
      ) : null}
    </div>
  );
}

/** 一轮里某一部分做了什么，说成一句话。 */
function describe(round: KbSyncRound): string {
  const bits: string[] = [];
  const kb = round.kb;
  if (kb) {
    const renders = Object.entries(kb.renders ?? {});
    if (renders.length) {
      bits.push(
        `重新渲染 ${renders.reduce((total, [, count]) => total + count, 0)} 页（` +
          renders.map(([reason, count]) => `${RENDERS[reason] ?? reason} ${count}`).join("、") +
          "）",
      );
    }
    if (kb.added || kb.removed) bits.push(`新内容 ${kb.added ?? 0} 片、去掉 ${kb.removed ?? 0} 片`);
    if (kb.embedded) bits.push(`新算向量 ${kb.embedded} 片`);
    if (kb.upserted || kb.deleted) bits.push(`入库写入 ${kb.upserted ?? 0}、删除 ${kb.deleted ?? 0}`);
    if (kb.stale) bits.push(`${kb.stale} 份没取下来、沿用旧内容`);
    if (kb.failed) bits.push(`${kb.failed} 篇新文档没取下来、下一轮再取`);
  }
  const images = round.images;
  if (images) {
    const parts: string[] = [];
    if (images.downloaded) parts.push(`下载 ${images.downloaded} 张`);
    if (images.embedded) parts.push(`新算向量 ${images.embedded} 张`);
    if (images.upserted || images.deleted) {
      parts.push(`入库写入 ${images.upserted ?? 0}、删除 ${images.deleted ?? 0}`);
    }
    bits.push(parts.length ? `图库：${parts.join("、")}` : "图库对过了，没有新图");
  }
  return bits.join("；") || "—";
}

function changesOf(round: KbSyncRound): string {
  const entries = Object.entries(round.changes ?? {});
  return entries.length ? entries.map(([kind, count]) => `${kind} ${count}`).join("、") : "—";
}

export function SyncStatus({
  sync,
  wiki,
}: {
  sync: KbSync | null;
  /** 挂了不止一个 wiki 时：这是哪个 wiki 的（每个 wiki 各更新各的，各有一份状态） */
  wiki?: { id: string; label: string };
}) {
  // 手动放行的命令：挂了不止一个 wiki 时指明是哪一个
  const only = wiki ? `--wiki ${wiki.id} ` : "";
  if (!sync) {
    return (
      <div className="text-muted-foreground px-1 text-xs leading-relaxed">
        {wiki ? `「${wiki.label}」的` : ""}自动更新还没有跑过。它是服务器上单独的一个服务（kbsync），每隔一段时间看 wiki
        改了什么、只重做变了的部分；本机调试时没有这个服务。跑起来以后，这里会显示它上次什么时候看过
        wiki、更新了什么。
      </div>
    );
  }

  const problems = Object.entries(sync.problems).filter(([, text]) => text);
  const late = overdue(sync);
  const state: { text: string; tone: Tone } = !sync.enabled
    ? { text: "已关闭", tone: "muted" }
    : problems.length
      ? { text: "有步骤没做成", tone: "warning" }
      : late
        ? { text: "好像没在跑", tone: "warning" }
        : { text: "正常", tone: "success" };
  const minutes = Math.round(sync.interval_seconds / 60);
  const rounds = [...sync.rounds].reverse().slice(0, 8);
  const waiting: ReactNode[] = [];
  if (sync.pending.fresh_titles.length) {
    waiting.push(
      <>等着重新渲染（它们的 Data 改了，上一轮没取下来）：{sync.pending.fresh_titles.join("、")}</>,
    );
  }
  if (sync.pending.full_started_at) {
    waiting.push(
      <>{formatDateTime(sync.pending.full_started_at)} 开始的全部重取还没做完，下一个夜间时段接着做</>,
    );
  } else if (sync.pending.shared_data_at) {
    waiting.push(
      <>
        公共数据集在 {formatDateTime(sync.pending.shared_data_at)}{" "}
        改过，说不清哪些页面跟着变：满一天以后的夜间时段会把渲染的页面全部重取一遍
      </>,
    );
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            自动更新{wiki ? ` · ${wiki.label}` : ""}{" "}
            <ToneBadge tone={state.tone}>{state.text}</ToneBadge>
          </CardTitle>
          <CardDescription>
            每 {minutes} 分钟看一次 wiki 的最近更改，只重做变了的页面和图片，再导入库里；机器人不用停
          </CardDescription>
        </div>
        <Button asChild variant="ghost" size="xs">
          <Link to={CONFIG_LINK}>
            去改这些参数（sync 一段） <ArrowRight />
          </Link>
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-4">
          <Fact label="上次看 wiki" value={sync.checked_at} />
          <Fact label="上次内容有更新" value={sync.updated_at} />
          <Fact label="上次全部重取" value={sync.full_at} />
          <Fact label="上次对图库" value={sync.images_at} />
        </div>

        {!sync.enabled ? (
          <Banner tone="info" className="mb-0">
            自动更新在配置里关掉了（sync.enabled），知识库不会跟着 wiki 更新。
          </Banner>
        ) : null}
        {late ? (
          <Banner tone="warning" className="mb-0">
            <b>上次看 wiki 是 {formatAgo(sync.checked_at)}，</b>
            按配置应该每 {minutes} 分钟看一次。kbsync 服务可能没在跑：到服务器上看{" "}
            <code className="font-mono text-xs">/opt/jianshanagent/ctl ps</code> 和{" "}
            <code className="font-mono text-xs">ctl logs kbsync</code>。
          </Banner>
        ) : null}
        {problems.map(([part, text]) => (
          <Banner key={part} tone="warning" className="mb-0">
            <b>{PARTS[part] ?? part}这一步没做成，每一轮都会再试：</b>
            <div className="mt-0.5 text-xs break-words">{text}</div>
            {part !== "check" ? (
              <div className="text-muted-foreground mt-1 text-xs">
                如果是被闸门拦下的、看过确实没问题，到服务器上手动放行一次：
                <code className="ml-1 font-mono">
                  ctl exec kbsync python -m jianshanagent.plugins.knowledge {only}sync --force
                </code>
              </div>
            ) : null}
          </Banner>
        ))}
        {waiting.length ? (
          <ul className="text-muted-foreground list-disc space-y-0.5 pl-5 text-xs leading-relaxed">
            {waiting.map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ul>
        ) : null}

        {rounds.length ? (
          <div className="-mx-4 -mb-2">
            <Table>
              <thead>
                <tr>
                  <Th>时间</Th>
                  <Th>结果</Th>
                  <Th>wiki 上新的改动</Th>
                  <Th>做了什么</Th>
                  <Th className="text-right">请求</Th>
                  <Th className="text-right">用时</Th>
                </tr>
              </thead>
              <tbody>
                {rounds.map((round) => {
                  const result = RESULTS[round.result] ?? { text: round.result, tone: "muted" };
                  return (
                    <Tr key={round.at}>
                      <Td className="text-muted-foreground text-xs whitespace-nowrap tabular-nums">
                        {formatTime(round.at, { seconds: false })}
                      </Td>
                      <Td className="whitespace-nowrap">
                        <ToneBadge tone={result.tone}>{result.text}</ToneBadge>
                      </Td>
                      <Td className="text-xs" title={(round.titles ?? []).join("、")}>
                        {changesOf(round)}
                      </Td>
                      <Td className="text-xs leading-relaxed">
                        {describe(round)}
                        {round.problems?.length ? (
                          <div className="text-warning mt-0.5 break-words">
                            {round.problems.join("；")}
                          </div>
                        ) : null}
                      </Td>
                      <Td className="text-right text-xs tabular-nums">
                        {formatNumber(round.requests)}
                      </Td>
                      <Td className="text-muted-foreground text-right text-xs whitespace-nowrap tabular-nums">
                        {formatSeconds(round.seconds)}
                      </Td>
                    </Tr>
                  );
                })}
              </tbody>
            </Table>
            <div className="text-muted-foreground px-4 pt-2 pb-1 text-[11px]">
              只列做了事或者出了问题的轮次；wiki 没有改动的那些轮次各发 1 个请求，不记在这里。「请求」是那一轮向
              wiki 发的请求数。
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
