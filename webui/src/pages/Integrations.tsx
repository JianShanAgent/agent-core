import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronRight, Copy, Plus } from "lucide-react";
import { type FormEvent, useRef, useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/form";
import { Table, Td, Th, Tr } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import type {
  IntegrationApp,
  IntegrationAppUsage,
  IntegrationModelUsage,
  IntegrationUsage,
  IntegrationUsageRange,
} from "@/lib/types";

const APPS_KEY = ["integrations", "apps"] as const;
const USAGE_KEY = ["integrations", "usage"] as const;

function AppIdentity({ id, name }: { id: string; name?: string }) {
  return (
    <div className="min-w-0 break-all">
      {name && name !== id ? <div className="font-medium">{name}</div> : null}
      <code className="text-muted-foreground text-xs">{id}</code>
    </div>
  );
}

function UsageNumber({ value, warn = false }: { value: number | null; warn?: boolean }) {
  return value === null ? (
    <span className="text-muted-foreground">未知</span>
  ) : warn && value > 0 ? (
    <Badge tone="warning">{formatNumber(value)}</Badge>
  ) : (
    <span className="tabular-nums">{formatNumber(value)}</span>
  );
}

const COUNT_COLUMNS = {
  prompt_tokens: "输入 Token",
  completion_tokens: "输出 Token",
  cached_tokens: "缓存 Token",
  reasoning_tokens: "推理 Token",
  embedding_tokens: "嵌入 Token",
  search_tokens: "搜索 Token",
  searches: "搜索次数",
} as const;

type CountKey = keyof typeof COUNT_COLUMNS;

function UsageMetric({ row, field }: { row: IntegrationAppUsage | IntegrationModelUsage; field: CountKey }) {
  const missing = field === "searches" ? row.unknown_searches : field === "search_tokens" ? row.unknown_search_tokens : 0;
  return (
    <>
      <UsageNumber value={row[field]} />
      {missing > 0 ? <span className="text-muted-foreground ml-1 whitespace-nowrap">+ 未知（{missing} 次调用）</span> : null}
    </>
  );
}

const MODEL_GROUPS: { kind: IntegrationModelUsage["kind"]; title: string; counts: CountKey[] }[] = [
  { kind: "llm", title: "语言模型", counts: ["prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens"] },
  { kind: "embedding", title: "嵌入模型", counts: ["embedding_tokens"] },
  { kind: "search", title: "搜索", counts: ["search_tokens", "searches"] },
];

function UsageDetail({
  appId,
  name,
  usage,
  onBack,
}: {
  appId: string;
  name?: string;
  usage: IntegrationUsage;
  onBack: () => void;
}) {
  const totals = usage.apps.find((row) => row.app_id === appId);
  const models = usage.models.filter((row) => row.app_id === appId);
  const metrics: [string, number, boolean?][] = totals ? [
    ["请求数", totals.requests],
    ["模型与搜索调用", totals.model_calls],
    ["用量未知的调用", totals.unknown_calls, true],
  ] : [];

  return (
    <div className="space-y-3 border-t p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">应用用量明细</h3>
          <AppIdentity id={appId} name={name} />
        </div>
        <Button size="sm" variant="outline" onClick={onBack}><ArrowLeft /> 返回应用概览</Button>
      </div>
      {totals ? (
        <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-3 xl:grid-cols-5">
          {metrics.map(([label, value, warn]) => (
            <div key={label} className="bg-card px-3 py-2">
              <dt className="text-muted-foreground text-xs">{label}</dt>
              <dd className="mt-0.5 text-sm font-medium"><UsageNumber value={value} warn={warn} /></dd>
            </div>
          ))}
          {(Object.keys(COUNT_COLUMNS) as CountKey[]).map((field) => (
            <div key={field} className="bg-card px-3 py-2">
              <dt className="text-muted-foreground text-xs">{COUNT_COLUMNS[field]}</dt>
              <dd className="mt-0.5 text-sm font-medium"><UsageMetric row={totals} field={field} /></dd>
            </div>
          ))}
        </dl>
      ) : null}
      {models.length ? MODEL_GROUPS.map(({ kind, title, counts }) => {
        const rows = models.filter((row) => row.kind === kind);
        if (!rows.length) return null;
        return (
          <section key={kind} className="rounded-lg border">
            <h4 className="px-3 pt-3 text-sm font-medium">{title}</h4>
            <Table aria-label={`${title}用量`}>
              <thead><tr>
                <Th scope="col">模型 / 服务</Th>
                <Th scope="col" className="text-right">调用</Th>
                <Th scope="col" className="text-right">用量未知</Th>
                {counts.map((key) => <Th key={key} scope="col" className="text-right">{COUNT_COLUMNS[key]}</Th>)}
              </tr></thead>
              <tbody>
                {rows.map((row) => (
                  <Tr key={row.model}>
                    <Td><code className="text-xs break-all">{row.model || "未标记"}</code></Td>
                    <Td className="text-right"><UsageNumber value={row.calls} /></Td>
                    <Td className="text-right"><UsageNumber value={row.unknown_calls} warn /></Td>
                    {counts.map((key) => <Td key={key} className="text-right"><UsageMetric row={row} field={key} /></Td>)}
                  </Tr>
                ))}
              </tbody>
            </Table>
          </section>
        );
      }) : <EmptyState title={totals ? "该时段没有模型或搜索用量明细" : "该时段没有此应用的用量记录"} />}
    </div>
  );
}

function UsageOverview({
  ids,
  names,
  rows,
  onSelect,
}: {
  ids: string[];
  names: Map<string, string>;
  rows: Map<string, IntegrationAppUsage>;
  onSelect: (appId: string) => void;
}) {
  if (!ids.length) return <EmptyState title="该时段没有第三方应用用量记录" />;
  return (
    <Table aria-label="各应用用量概览" className="border-t">
      <thead><tr>
        <Th scope="col">应用</Th>
        <Th scope="col" className="text-right">请求数</Th>
        <Th scope="col" className="text-right">模型与搜索调用</Th>
        <Th scope="col" className="text-right">用量未知</Th>
        <Th scope="col" className="text-right">明细</Th>
      </tr></thead>
      <tbody>
        {ids.map((id) => {
          const row = rows.get(id);
          return (
            <Tr key={id}>
              <Td><AppIdentity id={id} name={names.get(id)} /></Td>
              {row ? <>
                <Td className="text-right"><UsageNumber value={row.requests} /></Td>
                <Td className="text-right"><UsageNumber value={row.model_calls} /></Td>
                <Td className="text-right"><UsageNumber value={row.unknown_calls} warn /></Td>
              </> : <Td colSpan={3} className="text-muted-foreground text-right text-xs">无记录</Td>}
              <Td className="text-right">
                <Button variant="ghost" size="xs" aria-label={`查看 ${id} 用量明细`} onClick={() => onSelect(id)}>
                  查看明细 <ChevronRight />
                </Button>
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}

const PRESETS = [["today", "今天"], ["7d", "近 7 天"], ["30d", "近 30 天"]] as const;

function Usage({ apps }: { apps: IntegrationApp[] }) {
  const [selected, select] = useState<string | null>(null);
  const [range, setRange] = useState<IntegrationUsageRange>({ range: "today" });
  const [customizing, setCustomizing] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const inverted = Boolean(from && to && from > to);
  const query = useQuery({ queryKey: [...USAGE_KEY, range], queryFn: () => api.integrationUsage(range) });
  const usage = query.data;
  const names = new Map(apps.map((app) => [app.app_id, app.name]));
  const rows = new Map(usage?.apps.map((row) => [row.app_id, row]));
  const ids = [...new Set([...names.keys(), ...rows.keys(), ...(usage?.models.map((row) => row.app_id) ?? [])])];
  const apply = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (from && to && !inverted) setRange({ from, to });
  };

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>应用用量</CardTitle>
          <CardDescription>仅统计已记录的用量，不用于推算费用。</CardDescription>
        </div>
        <div role="group" aria-label="统计时段" className="flex flex-wrap gap-1">
          {PRESETS.map(([value, label]) => {
            const active = !customizing && "range" in range && range.range === value;
            return (
              <Button key={value} size="xs" variant={active ? "secondary" : "ghost"} aria-pressed={active} onClick={() => {
                setCustomizing(false);
                setRange({ range: value });
              }}>{label}</Button>
            );
          })}
          <Button size="xs" variant={customizing ? "secondary" : "ghost"} aria-expanded={customizing} onClick={() => setCustomizing(true)}>
            自定义
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {customizing ? (
          <form className="flex flex-wrap items-end gap-2" onSubmit={apply}>
            <div className="space-y-1">
              <Label htmlFor="integration-usage-from" className="text-xs">起始日期</Label>
              <Input id="integration-usage-from" type="date" value={from} onChange={(event) => setFrom(event.target.value)} aria-invalid={inverted} required />
            </div>
            <div className="space-y-1">
              <Label htmlFor="integration-usage-to" className="text-xs">结束日期</Label>
              <Input id="integration-usage-to" type="date" value={to} onChange={(event) => setTo(event.target.value)} aria-invalid={inverted} required />
            </div>
            <Button type="submit" variant="outline" disabled={!from || !to || inverted}>应用</Button>
            <p className="text-muted-foreground basis-full text-xs" role={inverted ? "alert" : undefined}>
              {inverted ? "起始日期不能晚于结束日期。" : "请选择起始日期和结束日期。"}
            </p>
          </form>
        ) : null}
        {usage ? (
          <div className="text-muted-foreground space-y-1 text-xs leading-relaxed">
            <p><span className="text-foreground font-medium">当前时段：</span>{usage.from} 至 {usage.to}（含首尾，{usage.timezone}）</p>
            <p>{usage.tracking_since ? `用量追踪起点：${formatDateTime(usage.tracking_since)}（本机时间）；此前不计入统计。` : "用量追踪起点：暂无记录。"}</p>
            <p>“用量未知”表示未取得用量的调用次数。数值只汇总已记录部分；0 不包含未知用量，“无记录”也不等于 0。</p>
          </div>
        ) : null}
      </CardContent>
      {query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : !usage ? <LoadingRows rows={3} /> : selected ? (
        <UsageDetail appId={selected} name={names.get(selected)} usage={usage} onBack={() => select(null)} />
      ) : (
        <UsageOverview ids={ids} names={names} rows={rows} onSelect={select} />
      )}
    </Card>
  );
}

function AppForm({ app, busy, onClose, onSave }: {
  app?: IntegrationApp;
  busy: boolean;
  onClose: () => void;
  onSave: (id: string, name: string) => void;
}) {
  const [id, setId] = useState(app?.app_id ?? "");
  const [name, setName] = useState(app?.name ?? "");
  const normalizedId = id.trim();
  const normalizedName = name.trim();
  const validId = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(normalizedId);
  const validName = Boolean(normalizedName && [...normalizedName].length <= 80);
  const canSave = (Boolean(app) || validId) && validName && normalizedName !== app?.name;
  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{app ? "编辑应用名称" : "添加第三方应用"}</DialogTitle>
          <DialogDescription>{app ? "应用 ID 和访问令牌不变。" : "请只为已批准接入的应用创建令牌。应用 ID 创建后不可修改。"}</DialogDescription>
        </DialogHeader>
        <form className="space-y-3" onSubmit={(event) => {
          event.preventDefault();
          if (canSave && !busy) onSave(normalizedId, normalizedName);
        }}>
          <div className="space-y-1.5">
            <Label htmlFor="integration-app-id">应用 ID</Label>
            <Input id="integration-app-id" value={id} disabled={Boolean(app) || busy} onChange={(event) => setId(event.target.value)} autoComplete="off" spellCheck={false} required aria-invalid={Boolean(id) && !validId} aria-describedby={app ? undefined : "integration-app-id-hint"} />
            {app ? null : <p id="integration-app-id-hint" className="text-muted-foreground text-xs">以英文字母或数字开头，可包含点、下划线和短横线，最多 64 个字符。</p>}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="integration-name">应用名称</Label>
            <Input id="integration-name" value={name} disabled={busy} onChange={(event) => setName(event.target.value)} required aria-invalid={Boolean(name) && !validName} aria-describedby="integration-name-hint" />
            <p id="integration-name-hint" className="text-muted-foreground text-xs">最多 80 个字符。</p>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={onClose}>取消</Button>
            <Button type="submit" loading={busy} disabled={!canSave}>{app ? "保存" : "创建应用"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function TokenDialog({ app, token, onClose }: { app: IntegrationApp; token: string; onClose: () => void }) {
  const field = useRef<HTMLInputElement>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "manual">("idle");
  const selectToken = () => {
    field.current?.focus();
    field.current?.select();
  };
  const copy = async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(token);
      } else {
        selectToken();
        if (!document.execCommand("copy")) throw new Error("copy failed");
      }
      setCopyState("copied");
    } catch {
      selectToken();
      setCopyState("manual");
    }
  };
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent onPointerDownOutside={(event) => event.preventDefault()} onEscapeKeyDown={(event) => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>保存访问令牌</DialogTitle>
          <DialogDescription>{app.name}（{app.app_id}）的令牌只显示这一次。请复制到安全的位置；关闭后不能再次查看。</DialogDescription>
        </DialogHeader>
        <Label htmlFor="integration-token">访问令牌</Label>
        <div className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
          <Input ref={field} id="integration-token" className="font-mono" value={token} readOnly autoComplete="off" spellCheck={false} />
          <Button variant="outline" onClick={() => void copy()}><Copy /> 复制令牌</Button>
        </div>
        <p role="status" className="text-muted-foreground text-xs">
          {copyState === "copied" ? "令牌已复制。" : copyState === "manual" ? "无法自动复制，请手动复制已选中的令牌。" : ""}
        </p>
        <DialogFooter><Button onClick={onClose}>关闭</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type DialogState =
  | { kind: "create" }
  | { kind: "rename"; app: IntegrationApp }
  | { kind: "disable" | "rotate"; app: IntegrationApp }
  | { kind: "token"; app: IntegrationApp; token: string };

type Change =
  | { kind: "create"; id: string; name: string }
  | { kind: "rename"; id: string; name: string }
  | { kind: "disable" | "enable" | "rotate"; id: string };

export default function Integrations() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: APPS_KEY, queryFn: api.integrations });
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [busy, setBusy] = useState(false);
  const managed = query.data?.managed === true;
  const apps = query.data?.apps ?? [];
  const close = () => setDialog(null);

  async function change(action: Change) {
    if (!managed || busy) return;
    setBusy(true);
    try {
      // 创建和轮换的响应包含明文令牌，不能交给 Query / Mutation 缓存。
      if (action.kind === "create" || action.kind === "rotate") {
        const { app, token } = action.kind === "create"
          ? await api.createIntegration({ app_id: action.id, name: action.name })
          : await api.rotateIntegrationToken(action.id);
        setDialog({ kind: "token", app, token });
      } else {
        await api.updateIntegration(action.id, action.kind === "rename" ? { name: action.name } : { enabled: action.kind === "enable" });
        close();
        toast.success(action.kind === "rename" ? "应用名称已更新" : action.kind === "enable" ? "应用已启用" : "应用已停用");
      }
      await Promise.all([client.invalidateQueries({ queryKey: APPS_KEY }), client.invalidateQueries({ queryKey: USAGE_KEY })]);
    } catch (error) {
      toast.error("未能完成操作", { description: error instanceof Error ? error.message : undefined });
    } finally {
      setBusy(false);
    }
  }

  const confirming = dialog?.kind === "disable" || dialog?.kind === "rotate" ? dialog : null;
  const rotating = confirming?.kind === "rotate";
  return (
    <div className="min-w-0">
      <PageHeader
        title="第三方接入"
        description="管理获准接入的应用及其访问令牌，按应用查看用量。"
        actions={managed ? <Button variant="brand" onClick={() => setDialog({ kind: "create" })} disabled={busy}><Plus /> 添加应用</Button> : undefined}
      />
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>接入应用</CardTitle>
              {query.data && !managed ? <CardDescription>当前部署不支持在此管理应用。以下列表仅供查看。</CardDescription> : null}
            </div>
          </CardHeader>
          {query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : !query.data ? <LoadingRows rows={3} /> : !apps.length ? (
            <EmptyState title="暂无接入应用" />
          ) : (
            <Table aria-label="接入应用列表">
              <thead><tr>
                <Th scope="col">应用</Th>
                <Th scope="col">状态</Th>
                <Th scope="col">创建时间</Th>
                <Th scope="col">最近请求</Th>
                {managed ? <Th scope="col" className="text-right">操作</Th> : null}
              </tr></thead>
              <tbody>
                {apps.map((app) => (
                  <Tr key={app.app_id}>
                    <Td><AppIdentity id={app.app_id} name={app.name} /></Td>
                    <Td><Badge tone={app.enabled ? "success" : "muted"}>{app.enabled ? "启用" : "停用"}</Badge></Td>
                    <Td className="text-muted-foreground text-xs whitespace-nowrap">{formatDateTime(app.created_at)}</Td>
                    <Td className="text-muted-foreground text-xs whitespace-nowrap">{formatDateTime(app.last_request_at)}</Td>
                    {managed ? (
                      <Td>
                        <div className="flex justify-end gap-0.5">
                          <Button size="xs" variant="ghost" disabled={busy} onClick={() => setDialog({ kind: "rename", app })}>改名</Button>
                          <Button size="xs" variant="ghost" disabled={busy} onClick={() => setDialog({ kind: "rotate", app })}>轮换令牌</Button>
                          <Button size="xs" variant="ghost" disabled={busy} onClick={() => {
                            if (app.enabled) setDialog({ kind: "disable", app });
                            else void change({ kind: "enable", id: app.app_id });
                          }}>{app.enabled ? "停用" : "启用"}</Button>
                        </div>
                      </Td>
                    ) : null}
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
        {managed ? <Usage apps={apps} /> : query.data ? (
          <Card><EmptyState title="当前部署不支持用量统计" hint="配置 PostgreSQL 后可按应用查看用量。" /></Card>
        ) : null}
      </div>
      {dialog?.kind === "create" || dialog?.kind === "rename" ? (
        <AppForm
          key={dialog.kind === "rename" ? dialog.app.app_id : "create"}
          app={dialog.kind === "rename" ? dialog.app : undefined}
          busy={busy}
          onClose={close}
          onSave={(id, name) => void change({ kind: dialog.kind, id, name })}
        />
      ) : null}
      {confirming ? (
        <ConfirmDialog
          open
          onOpenChange={(open) => { if (!open && !busy) close(); }}
          title={rotating ? "轮换访问令牌？" : "停用此应用？"}
          description={<>
            {confirming.app.name}（{confirming.app.app_id}）：{rotating
              ? "旧令牌会失效，新令牌只显示一次。请准备好更新接入方的配置。"
              : "停用后将拒绝该应用的请求，历史用量仍然可以查看。"}
          </>}
          confirmText={rotating ? "轮换令牌" : "停用应用"}
          destructive
          loading={busy}
          onConfirm={() => void change({ kind: confirming.kind, id: confirming.app.app_id })}
        />
      ) : null}
      {dialog?.kind === "token" ? <TokenDialog app={dialog.app} token={dialog.token} onClose={close} /> : null}
    </div>
  );
}
