// 首页：机器人的现状 + 一段时间里的统计（消息、查证、用时、模型用量）+ 最近的消息。

import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  CircleCheck,
  CircleX,
  Coins,
  DatabaseZap,
  MessageSquare,
  Timer,
  Users,
  Waypoints,
} from "lucide-react";
import { Link, useNavigate } from "react-router";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AXIS_TICK, ChartTip, Donut, GRID_STROKE, type TipProps } from "@/components/charts";
import { EmptyState, ErrorState, LoadingRows, PageHeader, StatCard } from "@/components/common";
import { TraceTable } from "@/components/TraceTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton, Table, Td, Th, Tr } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import {
  formatAgo,
  formatBucket,
  formatNumber,
  formatPercent,
  formatSeconds,
  formatTokens,
  formatUptime,
} from "@/lib/format";
import { useStored } from "@/lib/hooks";
import { label, RANGES, ROUTE_LABELS, STEP_LABELS } from "@/lib/labels";
import type { Overview, OverviewStats, Status, UsageRow } from "@/lib/types";
import { cn } from "@/lib/utils";

const FEATURES: [string, string][] = [
  ["research", "查证工作流"],
  ["knowledge", "知识库检索"],
  ["image_search", "以图搜图"],
  ["memory", "记忆"],
  ["chatlog", "群聊记录"],
  ["tracing", "处理记录落库"],
];

function StatusCard({ status }: { status: Status }) {
  const gateway = status.gateway;
  const line = status.dry_run
    ? { dot: "bg-warning", text: "调试模式：没有连 QQ" }
    : gateway?.connected
      ? { dot: "bg-success", text: "QQ 已连接" }
      : { dot: "bg-destructive", text: "QQ 连接断开" };
  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-3 p-4">
        <div className="flex items-center gap-3">
          <span className="relative flex size-2.5">
            {gateway?.connected ? (
              <span className="bg-success absolute inline-flex size-full animate-ping rounded-full opacity-60" />
            ) : null}
            <span className={cn("relative inline-flex size-2.5 rounded-full", line.dot)} />
          </span>
          <div>
            <div className="text-sm font-medium">{line.text}</div>
            <div className="text-muted-foreground text-xs">
              已运行 {formatUptime(status.uptime_seconds)}
              {gateway
                ? ` · 收到 ${formatNumber(gateway.events)} 个事件` +
                  (gateway.last_event_at ? `，最近一个在${formatAgo(gateway.last_event_at)}` : "") +
                  (gateway.disconnects ? ` · 断过 ${gateway.disconnects} 次` : "")
                : ""}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {FEATURES.map(([key, name]) =>
            status.features[key] ? (
              <Badge key={key} tone="success">
                <CircleCheck /> {name}
              </Badge>
            ) : (
              <Badge key={key} tone="muted">
                <CircleX /> {name}
              </Badge>
            ),
          )}
        </div>
        <div className="text-muted-foreground ml-auto text-xs">
          {status.release ? `发布 ${status.release}` : `v${status.version}`} · 第{" "}
          {status.generation} 次装配
          {status.generation > 1 ? `（${formatAgo(status.loaded_at)}）` : ""}
        </div>
      </CardContent>
    </Card>
  );
}

function UsageTable({
  rows,
  first,
}: {
  rows: (UsageRow & { name: string; note?: string })[];
  first: string;
}) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>{first}</Th>
          <Th className="text-right">调用</Th>
          <Th className="text-right">没成</Th>
          <Th className="text-right">输入 / 输出</Th>
          <Th className="text-right">平均</Th>
          <Th className="text-right">九成在</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <Tr key={row.name}>
            <Td>
              <div className="font-medium">{row.name}</div>
              {row.note ? <div className="text-muted-foreground text-[11px]">{row.note}</div> : null}
            </Td>
            <Td className="text-right tabular-nums">{formatNumber(row.calls)}</Td>
            <Td className={cn("text-right tabular-nums", row.failed > 0 && "text-destructive")}>
              {row.failed ? `${row.failed}（${formatPercent(row.failed, row.calls)}）` : "0"}
            </Td>
            <Td className="text-muted-foreground text-right text-xs tabular-nums">
              {formatTokens(row.prompt_tokens)} / {formatTokens(row.completion_tokens)}
            </Td>
            <Td className="text-right tabular-nums">{formatSeconds(row.avg_seconds)}</Td>
            <Td className="text-right tabular-nums">{formatSeconds(row.p90_seconds)}</Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

function Stats({ stats, data }: { stats: OverviewStats; data: Overview }) {
  const navigate = useNavigate();
  const { totals } = stats;
  const daily = data.range.daily;
  const tick = (value: string) => formatBucket(value, daily);
  const series = stats.series.map((point) => ({
    ...point,
    other: point.messages - point.research,
    // 这个时段没有查证：线断开，不画成 0 秒
    median_seconds: point.research ? point.median_seconds : null,
  }));
  const edges = data.duration_edges;
  const durations = stats.durations.map((count, index) => ({
    name:
      index === 0
        ? `< ${edges[0]} 秒`
        : index === edges.length
          ? `≥ ${edges[edges.length - 1]} 秒`
          : `${edges[index - 1]}–${edges[index]} 秒`,
    count,
  }));
  const empty = totals.messages === 0 && totals.llm_calls === 0;

  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="对机器人说的消息"
          value={formatNumber(totals.messages)}
          icon={MessageSquare}
          hint={`${formatNumber(totals.users)} 个人 · ${formatNumber(totals.groups)} 个群`}
          onClick={() => navigate("/traces")}
        />
        <StatCard
          label="查证"
          value={formatNumber(totals.research)}
          icon={Waypoints}
          hint={
            totals.research
              ? `查到 ${totals.answered} · 没查到 ${totals.not_found} · 失败 ${totals.failed}`
              : `命令 ${totals.commands} · 闲聊 ${totals.chitchat}`
          }
          onClick={() => navigate("/traces?route=research")}
        />
        <StatCard
          label="查证用时（中位数）"
          value={totals.research ? formatSeconds(totals.research_median_seconds) : "—"}
          icon={Timer}
          hint={
            totals.research
              ? `九成的题在 ${formatSeconds(totals.research_p90_seconds)} 内`
              : "这段时间没有查证"
          }
        />
        <StatCard
          label="出错"
          value={formatNumber(totals.errors + totals.failed)}
          icon={CircleX}
          tone={totals.errors + totals.failed > 0 ? "danger" : undefined}
          hint={
            totals.limited ? `另有 ${totals.limited} 条被限流` : `处理出错 ${totals.errors} · 查证失败 ${totals.failed}`
          }
          onClick={() => navigate("/traces?problems=1")}
        />
        <StatCard
          label="模型调用"
          value={formatNumber(totals.llm_calls)}
          icon={Activity}
          tone={totals.llm_failed > 0 ? "warning" : undefined}
          hint={
            totals.llm_failed
              ? `${totals.llm_failed} 次没成（超时或报错）`
              : "都正常返回了"
          }
          onClick={() => navigate("/calls")}
        />
        <StatCard
          label="输入 token"
          value={formatTokens(totals.prompt_tokens)}
          icon={Coins}
          hint={
            totals.cached_tokens
              ? `其中 ${formatPercent(totals.cached_tokens, totals.prompt_tokens)} 命中缓存`
              : "提示词和资料"
          }
        />
        <StatCard
          label="输出 token"
          value={formatTokens(totals.completion_tokens)}
          icon={Coins}
          hint={`其中思考 ${formatPercent(totals.reasoning_tokens, totals.completion_tokens)}`}
        />
        <StatCard
          label="全部消息用时（中位数）"
          value={totals.messages ? formatSeconds(totals.median_seconds) : "—"}
          icon={Users}
          hint={totals.messages ? `九成在 ${formatSeconds(totals.p90_seconds)} 内` : "—"}
        />
      </div>

      {empty ? (
        <Card>
          <EmptyState
            title="这段时间还没有消息"
            hint="处理记录是从这个功能上线那一刻开始记的。可以去「网页对话」发一句试试，或者把时段选长一点。"
            action={
              <Button asChild variant="outline" size="sm" className="mt-1">
                <Link to="/chat">去网页对话</Link>
              </Button>
            }
          />
        </Card>
      ) : (
        <>
          <div className="grid gap-3 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader>
                <div>
                  <CardTitle>消息量和查证用时</CardTitle>
                  <CardDescription>
                    柱子是消息数（{daily ? "按天" : "按小时"}），线是查证用时的中位数
                  </CardDescription>
                </div>
              </CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={240}>
                  <ComposedChart data={series} margin={{ top: 8, right: 0, bottom: 0, left: -18 }}>
                    <CartesianGrid vertical={false} stroke={GRID_STROKE} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="t"
                      tickFormatter={tick}
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      minTickGap={24}
                    />
                    <YAxis
                      yAxisId="count"
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      allowDecimals={false}
                    />
                    <YAxis
                      yAxisId="seconds"
                      orientation="right"
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      width={36}
                      unit="秒"
                    />
                    <Tooltip
                      cursor={{ fill: "var(--muted)", opacity: 0.5 }}
                      content={(props) => (
                        <ChartTip
                          {...(props as TipProps)}
                          title={tick}
                          format={(value, key) =>
                            key === "median_seconds"
                              ? formatSeconds(Number(value))
                              : formatNumber(Number(value))
                          }
                        />
                      )}
                    />
                    <Bar
                      yAxisId="count"
                      dataKey="research"
                      name="查证"
                      stackId="m"
                      fill="var(--chart-1)"
                      maxBarSize={28}
                      isAnimationActive={false}
                    />
                    <Bar
                      yAxisId="count"
                      dataKey="other"
                      name="命令和闲聊"
                      stackId="m"
                      fill="var(--chart-2)"
                      radius={[3, 3, 0, 0]}
                      maxBarSize={28}
                      isAnimationActive={false}
                    />
                    <Line
                      yAxisId="seconds"
                      dataKey="median_seconds"
                      name="查证用时中位数"
                      stroke="var(--chart-3)"
                      strokeWidth={2}
                      dot={{ r: 2.5, fill: "var(--chart-3)", strokeWidth: 0 }}
                      connectNulls
                      isAnimationActive={false}
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>查证花了多久</CardTitle>
                  <CardDescription>每一档有几个问题</CardDescription>
                </div>
              </CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart
                    data={durations}
                    layout="vertical"
                    margin={{ top: 0, right: 16, bottom: 0, left: 8 }}
                  >
                    <CartesianGrid horizontal={false} stroke={GRID_STROKE} strokeDasharray="3 3" />
                    <XAxis
                      type="number"
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      allowDecimals={false}
                    />
                    <YAxis
                      type="category"
                      dataKey="name"
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      width={70}
                    />
                    <Tooltip
                      cursor={{ fill: "var(--muted)", opacity: 0.5 }}
                      content={(props) => <ChartTip {...(props as TipProps)} />}
                    />
                    <Bar
                      dataKey="count"
                      name="问题数"
                      fill="var(--chart-1)"
                      radius={[0, 3, 3, 0]}
                      maxBarSize={18}
                      isAnimationActive={false}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          </div>

          <div className="grid gap-3 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader>
                <div>
                  <CardTitle>模型用量</CardTitle>
                  <CardDescription>
                    输入和输出的 token（{daily ? "按天" : "按小时"}）；输出里包含思考
                  </CardDescription>
                </div>
              </CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={200}>
                  <AreaChart data={series} margin={{ top: 8, right: 8, bottom: 0, left: -6 }}>
                    <CartesianGrid vertical={false} stroke={GRID_STROKE} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="t"
                      tickFormatter={tick}
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      minTickGap={24}
                    />
                    <YAxis
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      tickFormatter={(value: number) => formatTokens(value)}
                    />
                    <Tooltip
                      content={(props) => (
                        <ChartTip
                          {...(props as TipProps)}
                          title={tick}
                          format={(value) => formatNumber(Number(value))}
                        />
                      )}
                    />
                    <Area
                      dataKey="prompt_tokens"
                      name="输入"
                      stackId="t"
                      stroke="var(--chart-2)"
                      fill="var(--chart-2)"
                      fillOpacity={0.25}
                      isAnimationActive={false}
                    />
                    <Area
                      dataKey="completion_tokens"
                      name="输出"
                      stackId="t"
                      stroke="var(--chart-1)"
                      fill="var(--chart-1)"
                      fillOpacity={0.35}
                      isAnimationActive={false}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>用户都在问什么</CardTitle>
                  <CardDescription>查证的问题按类别分（分析问题那一步归的类）</CardDescription>
                </div>
              </CardHeader>
              <CardContent>
                {stats.topics.length ? (
                  <Donut
                    data={stats.topics.map((item) => ({ name: item.topic, value: item.count }))}
                  />
                ) : (
                  <Donut
                    data={stats.routes.map((item) => ({
                      name: label(ROUTE_LABELS, item.route),
                      value: item.count,
                    }))}
                  />
                )}
              </CardContent>
            </Card>
          </div>

          <div className="grid gap-3 xl:grid-cols-2">
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>各档模型</CardTitle>
                  <CardDescription>core.llm.yaml 里的档位各被调了多少次、多快</CardDescription>
                </div>
                <Button asChild variant="ghost" size="xs">
                  <Link to="/models">
                    模型管理 <ArrowRight />
                  </Link>
                </Button>
              </CardHeader>
              <UsageTable
                first="档位"
                rows={stats.profiles.map((row) => ({
                  ...row,
                  name: row.profile,
                  note: `${row.model}${row.thinking ? " · 开思考" : ""}`,
                }))}
              />
            </Card>
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>各个环节</CardTitle>
                  <CardDescription>时间和 token 花在处理流程的哪一步上</CardDescription>
                </div>
              </CardHeader>
              <UsageTable
                first="环节"
                rows={stats.purposes.map((row) => ({
                  ...row,
                  name: label(STEP_LABELS, row.purpose),
                }))}
              />
            </Card>
          </div>
        </>
      )}
    </>
  );
}

export default function Dashboard() {
  const [hours, setHours] = useStored("jianshanagent.dashboard.hours", 24);
  const [channel, setChannel] = useStored("jianshanagent.dashboard.channel", "");
  const query = useQuery({
    queryKey: ["overview", hours, channel],
    queryFn: () => api.overview(hours, channel || undefined),
    // 有消息正在处理时刷得勤一点
    refetchInterval: (state) => ((state.state.data?.active.length ?? 0) > 0 ? 2_000 : 10_000),
    placeholderData: (previous) => previous,
  });
  const data = query.data;

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="首页"
        description="机器人现在怎么样，这段时间处理了多少消息、花了多久、用了多少模型额度。"
        actions={
          <>
            <SimpleSelect
              size="sm"
              value={channel}
              onChange={setChannel}
              options={[
                { value: "", label: "全部来源" },
                { value: "qq", label: "只看 QQ" },
                { value: "web", label: "只看网页对话" },
              ]}
            />
            <div className="bg-muted flex items-center gap-0.5 rounded-lg p-[3px]">
              {RANGES.map((range) => (
                <button
                  key={range.hours}
                  type="button"
                  onClick={() => setHours(range.hours)}
                  className={cn(
                    "h-7 cursor-pointer rounded-md px-2.5 text-[13px] font-medium transition-colors",
                    hours === range.hours
                      ? "bg-card text-foreground shadow-xs"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {range.label}
                </button>
              ))}
            </div>
          </>
        }
      />
      {query.isPending ? (
        <>
          <Skeleton className="h-16" />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {Array.from({ length: 8 }, (_, index) => (
              <Skeleton key={index} className="h-24" />
            ))}
          </div>
          <LoadingRows />
        </>
      ) : query.isError || !data ? (
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      ) : (
        <>
          <StatusCard status={data.status} />
          {data.active.length ? (
            <Card className="border-info/40">
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <span className="relative flex size-2">
                    <span className="bg-info absolute inline-flex size-full animate-ping rounded-full opacity-60" />
                    <span className="bg-info relative inline-flex size-2 rounded-full" />
                  </span>
                  正在处理 {data.active.length} 条消息
                </CardTitle>
              </CardHeader>
              <TraceTable rows={data.active} compact />
            </Card>
          ) : null}
          {data.stats ? (
            <Stats stats={data.stats} data={data} />
          ) : (
            <Card>
              <EmptyState
                icon={DatabaseZap}
                title="没有配置数据库，做不了统计"
                hint="处理记录只在内存里留最近几十条（下面能看到），重启就没了。线上有数据库，统计是全的。"
              />
            </Card>
          )}
          <Card>
            <CardHeader>
              <div>
                <CardTitle>最近的消息</CardTitle>
                <CardDescription>点一条看它是怎么处理的</CardDescription>
              </div>
              <Button asChild variant="ghost" size="xs">
                <Link to="/traces">
                  全部 <ArrowRight />
                </Link>
              </Button>
            </CardHeader>
            {data.recent.length ? (
              <TraceTable rows={data.recent} compact />
            ) : (
              <EmptyState title="还没有消息" className="py-8" />
            )}
          </Card>
        </>
      )}
    </div>
  );
}
