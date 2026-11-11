// 系统信息：进程、连接、配置目录、凭据填没填、登录令牌、数据库的表和备份，以及重新加载 / 重启、界面主题。

import { useQuery } from "@tanstack/react-query";
import {
  CircleCheck,
  CircleX,
  DatabaseZap,
  Monitor,
  Moon,
  Power,
  RefreshCw,
  Sun,
} from "lucide-react";
import type { ComponentType, ReactNode } from "react";
import { Link } from "react-router";

import { EmptyState, ErrorState, Field, PageHeader } from "@/components/common";
import { Banner, useSystemActions } from "@/components/layout/StateBanners";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton, Table, Td, Th, Tr } from "@/components/ui/misc";
import { api } from "@/lib/api";
import {
  formatAgo,
  formatBytes,
  formatDateTime,
  formatNumber,
  formatUptime,
} from "@/lib/format";
import { type Theme, useTheme } from "@/lib/hooks";
import type { BackupState, Status, SystemInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

const FEATURES: [string, string][] = [
  ["research", "查证工作流"],
  ["knowledge", "知识库检索"],
  ["image_search", "以图搜图"],
  ["memory", "记忆"],
  ["chatlog", "群聊记录"],
  ["wiki", "wiki 查询"],
  ["tracing", "处理记录落库"],
];

const TABLE_NOTES: Record<string, string> = {
  traces: "处理记录",
  llm_calls: "模型调用",
  llm_messages: "对话历史",
  memories: "记忆",
  chat_messages: "群聊记录",
  kb_chunks: "知识库切片",
  kb_terms: "知识库的词表",
  kb_images: "图库",
  kb_meta: "知识库概况",
  admin_labels: "备注名",
  backup_runs: "每次备份的结果",
  schema_migrations: "已执行的数据库变更",
};

const THEMES: { value: Theme; label: string; icon: ComponentType<{ className?: string }> }[] = [
  { value: "light", label: "浅色", icon: Sun },
  { value: "dark", label: "深色", icon: Moon },
  { value: "system", label: "跟随系统", icon: Monitor },
];

/** 一张分区卡片：标题、一句说明、内容。 */
function Section({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader>
        <div>
          <CardTitle>{title}</CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">{children}</CardContent>
    </Card>
  );
}

/** 路径、键名这类要原样看的东西。 */
function Mono({ children }: { children: ReactNode }) {
  return <code className="font-mono text-xs break-all">{children}</code>;
}

/** 开了 / 没开 的徽标。 */
function OnOff({ on, children }: { on: boolean; children: ReactNode }) {
  return (
    <Badge tone={on ? "success" : "muted"}>
      {on ? <CircleCheck /> : <CircleX />} {children}
    </Badge>
  );
}

function Running({ info }: { info: SystemInfo }) {
  const { status, environment } = info;
  return (
    <Section title="运行">
      <Field label="版本">v{status.version}</Field>
      <Field label="发布号">
        {status.release ? (
          <Mono>{status.release}</Mono>
        ) : (
          <span className="text-muted-foreground">没有（按发布流程部署到线上的才有）</span>
        )}
      </Field>
      <Field label="启动时间">
        {formatDateTime(status.started_at)}
        <span className="text-muted-foreground">
          ，已运行 {formatUptime(status.uptime_seconds)}
        </span>
      </Field>
      <Field label="进程号">
        <span className="tabular-nums">{environment.pid}</span>
      </Field>
      <Field label="Python">
        {environment.python}
        <span className="text-muted-foreground">（{environment.platform}）</span>
      </Field>
      <Field label="容器">{environment.in_container ? "在容器里运行" : "不在容器里"}</Field>
      <Field label="时区">{status.timezone}</Field>
      <Field label="装配">
        第 {status.generation} 次，最近一次在 {formatDateTime(status.loaded_at)}（
        {formatAgo(status.loaded_at)}）
        <div className="text-muted-foreground mt-0.5 text-xs leading-relaxed">
          改配置后不重启进程、把插件重新装一遍，算一次装配。
        </div>
      </Field>
    </Section>
  );
}

function Gateway({ status }: { status: Status }) {
  const gateway = status.gateway;
  if (!gateway) {
    return status.dry_run ? (
      <Badge tone="warning">调试模式，没有连 QQ</Badge>
    ) : (
      <Badge tone="muted">还没有开始连</Badge>
    );
  }
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {gateway.connected ? (
          <Badge tone="success">
            <CircleCheck /> 连着
          </Badge>
        ) : (
          <Badge tone="danger">
            <CircleX /> 断开
          </Badge>
        )}
        {gateway.connected_at ? (
          <span>
            {gateway.connected ? "" : "上一次"}
            {formatDateTime(gateway.connected_at)} 连上的（{formatAgo(gateway.connected_at)}）
          </span>
        ) : (
          <span className="text-muted-foreground">还没有连上过</span>
        )}
      </div>
      <div className="text-muted-foreground text-xs leading-relaxed">
        {gateway.disconnects ? `断过 ${gateway.disconnects} 次` : "没有断过"} · 收到过{" "}
        {formatNumber(gateway.events)} 个事件
        {gateway.last_event_at
          ? `，最近一个在 ${formatDateTime(gateway.last_event_at)}（${formatAgo(gateway.last_event_at)}）`
          : ""}
      </div>
    </div>
  );
}

function Connections({ status }: { status: Status }) {
  return (
    <Section title="连接">
      <Field label="QQ">
        <Gateway status={status} />
      </Field>
      <Field label="数据库">
        <OnOff on={status.database}>{status.database ? "有" : "没有"}</OnOff>
        {status.database ? null : (
          <span className="text-muted-foreground ml-2 text-xs">
            统计、模型调用这些要查库的页面看不了
          </span>
        )}
      </Field>
      <Field label="大模型">
        <OnOff on={status.llm}>{status.llm ? "启用" : "没启用"}</OnOff>
      </Field>
      <Field label="各项能力">
        <div className="flex flex-wrap gap-1.5">
          {FEATURES.map(([key, name]) => (
            <OnOff key={key} on={Boolean(status.features[key])}>
              {name}
            </OnOff>
          ))}
        </div>
      </Field>
    </Section>
  );
}

function ConfigLinks({ names }: { names: string[] }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1">
      {names.map((name) => (
        <Link
          key={name}
          to={`/config/${name}`}
          className="text-brand font-mono text-xs hover:underline"
        >
          {name}
        </Link>
      ))}
    </div>
  );
}

function Configuration({ info }: { info: SystemInfo }) {
  const { environment } = info;
  const state = info.status.config;
  return (
    <Section title="配置">
      <Field label="配置目录">
        <Mono>{environment.config_dir}</Mono>
        <div className="text-muted-foreground mt-0.5 text-xs">默认值，跟着代码一起发布</div>
      </Field>
      {environment.overrides_dir ? (
        <Field label="覆写目录">
          <Mono>{environment.overrides_dir}</Mono>
          <div className="text-muted-foreground mt-0.5 text-xs">
            网页上改的配置存在这里，只记和默认值不一样的项
          </div>
        </Field>
      ) : (
        <Banner tone="warning" className="my-1">
          没设置覆写目录（环境变量 JIANSHANAGENT_CONFIG_OVERRIDES_DIR），网页上改不了配置
        </Banner>
      )}
      <Field label="改过的配置文件">
        {state.overrides.length ? (
          <ConfigLinks names={state.overrides} />
        ) : (
          <span className="text-muted-foreground">没有，全部是默认值</span>
        )}
      </Field>
      <Field label="要重启才生效的">
        {state.pending_restart.length ? (
          <>
            <ConfigLinks names={state.pending_restart} />
            <div className="text-muted-foreground mt-0.5 text-xs">
              这几份改过了，但进程还在用启动时读到的那一版
            </div>
          </>
        ) : (
          <span className="text-muted-foreground">没有</span>
        )}
      </Field>
      {state.pending_reload.length ? (
        <Field label="重新加载后生效的">
          <ConfigLinks names={state.pending_reload} />
        </Field>
      ) : null}
    </Section>
  );
}

function Secrets({ secrets }: { secrets: SystemInfo["secrets"] }) {
  return (
    <Section
      title="凭据"
      description={
        <>
          凭据的值不在网页上显示，也不能在这里改。要换凭据：改好开发机的{" "}
          <Mono>config/secrets.yaml</Mono>，运行 <Mono>deploy/scripts/release.sh secrets</Mono>{" "}
          上传，再重启机器人。
        </>
      }
    >
      {secrets.length ? (
        <div className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
          {secrets.map((item) => (
            <div key={item.key} className="flex min-w-0 items-center justify-between gap-2">
              <Mono>{item.key}</Mono>
              <Badge tone={item.set ? "success" : "muted"}>{item.set ? "已填" : "没填"}</Badge>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-muted-foreground text-sm">secrets.yaml 里一项都没有。</div>
      )}
    </Section>
  );
}

function LoginToken({ source }: { source: string }) {
  return (
    <Section title="登录">
      <Field label="令牌来源">
        {source === "secrets" ? (
          <>
            <Mono>secrets.yaml</Mono> 的 <Mono>admin.token</Mono>
          </>
        ) : source === "file" || source === "generated" ? (
          <>
            第一次启动时自动生成的，存在数据目录里的令牌文件里
            {source === "generated" ? "（就是这次启动生成的）" : ""}
          </>
        ) : (
          source || "—"
        )}
      </Field>
      <Field label="怎么换">
        在 <Mono>secrets.yaml</Mono> 里写一个 <Mono>admin.token</Mono>
        （至少 12 个字符，写了就以它为准），或者删掉令牌文件后重启，让它重新生成一个（新令牌写在那个文件里）。
        <div className="text-muted-foreground mt-0.5 text-xs">
          换了以后，已经登录的都要重新登录。
        </div>
      </Field>
    </Section>
  );
}

function Backup({ backup }: { backup: BackupState }) {
  const last = backup.last;
  return (
    <Section
      title="数据库备份"
      description={
        <>
          由服务器上的备份容器每天做一次，做完记一条结果，这里显示的就是它。
          {backup.warn_hours > 0
            ? `超过 ${backup.warn_hours} 小时没有成功的，页面顶上会提醒。`
            : "提醒关掉了（core.admin 的 backup_warn_hours 是 0）。"}
        </>
      }
    >
      {last ? (
        <>
          <Field label="最近一次">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              {last.ok ? (
                <Badge tone="success">
                  <CircleCheck /> 成功
                </Badge>
              ) : (
                <Badge tone="danger">
                  <CircleX /> 没成
                </Badge>
              )}
              <span>
                {formatDateTime(last.finished_at)}（{formatAgo(last.finished_at)}）
              </span>
            </div>
            {last.detail ? (
              <div className={cn("mt-0.5 text-xs", last.ok ? "text-warning" : "text-destructive")}>
                {last.detail}
              </div>
            ) : null}
          </Field>
          {last.ok ? (
            <>
              <Field label="文件">
                <Mono>{last.file}</Mono>
                <span className="text-muted-foreground">，{formatBytes(last.bytes)}</span>
              </Field>
              <Field label="第二份">
                {last.copied ? (
                  "已另存到第二个存放处"
                ) : (
                  <span className="text-muted-foreground">
                    没有另存（没配第二个存放处，或者这次没拷成）
                  </span>
                )}
              </Field>
            </>
          ) : (
            <Field label="最近一次成功">
              {backup.last_ok_at ? (
                <>
                  {formatDateTime(backup.last_ok_at)}（{formatAgo(backup.last_ok_at)}）
                </>
              ) : (
                <span className="text-muted-foreground">还没有成功过</span>
              )}
            </Field>
          )}
        </>
      ) : (
        <div className="text-muted-foreground text-sm">
          还没有记录。第一次备份做完才会有；没有部署备份容器的地方（比如本机）一直是空的。
        </div>
      )}
    </Section>
  );
}

function Tables({ info }: { info: SystemInfo }) {
  const { tables } = info;
  const total = tables.reduce((sum, table) => sum + table.bytes, 0);
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>数据库的表</CardTitle>
          {tables.length ? (
            <CardDescription>
              一共 {tables.length} 张，占 {formatBytes(total)}；行数是数据库自己估的，不是精确值
            </CardDescription>
          ) : null}
        </div>
      </CardHeader>
      {!info.status.database ? (
        <EmptyState icon={DatabaseZap} title="没有配置数据库" className="py-8" />
      ) : tables.length === 0 ? (
        <EmptyState title="数据库里还没有表" className="py-8" />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>表</Th>
              <Th>存的是什么</Th>
              <Th className="text-right">行数</Th>
              <Th className="text-right">占用空间</Th>
            </tr>
          </thead>
          <tbody>
            {tables.map((table) => (
              <Tr key={table.name}>
                <Td>
                  <Mono>{table.name}</Mono>
                </Td>
                <Td className="text-muted-foreground text-xs">{TABLE_NOTES[table.name] ?? ""}</Td>
                <Td className="text-right tabular-nums">{formatNumber(table.rows)}</Td>
                <Td className="text-right whitespace-nowrap tabular-nums">
                  {formatBytes(table.bytes)}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

/** 一个按钮配一句说明：什么时候该用它。 */
function Action({ button, children }: { button: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start gap-x-3 gap-y-1.5">
      <div className="w-28 shrink-0">{button}</div>
      <p className="text-muted-foreground min-w-0 flex-1 basis-52 text-xs leading-relaxed">
        {children}
      </p>
    </div>
  );
}

function Operations() {
  const actions = useSystemActions();
  const { theme, setTheme } = useTheme();
  return (
    <Section title="操作">
      <Action
        button={
          <Button
            variant="outline"
            size="sm"
            className="w-full"
            loading={actions.reloading}
            disabled={actions.restarting}
            onClick={actions.reload}
          >
            {actions.reloading ? null : <RefreshCw />} 重新加载
          </Button>
        }
      >
        按盘上的配置重新装一遍插件，不断 QQ 连接。配置在盘上改了、机器人还没用上的时候用它（页面顶上会有提示）。
      </Action>
      <Action
        button={
          <Button
            variant="outline"
            size="sm"
            className="w-full"
            loading={actions.restarting}
            onClick={actions.askRestart}
          >
            {actions.restarting ? null : <Power />} {actions.restarting ? "重启中…" : "重启机器人"}
          </Button>
        }
      >
        进程原地重启，约十几秒，正在处理的消息会中断。有配置要重启才生效，或者机器人不对劲、重新加载也不管用的时候用它。
      </Action>
      {actions.confirmDialog}
      <Field label="主题" className="mt-1 border-t pt-3">
        <div className="bg-muted inline-flex items-center gap-0.5 rounded-lg p-[3px]">
          {THEMES.map((item) => (
            <button
              key={item.value}
              type="button"
              onClick={() => setTheme(item.value)}
              className={cn(
                "flex h-7 cursor-pointer items-center gap-1.5 rounded-md px-2.5 text-[13px] font-medium transition-colors",
                theme === item.value
                  ? "bg-card text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <item.icon className="size-3.5" />
              {item.label}
            </button>
          ))}
        </div>
        <div className="text-muted-foreground mt-1 text-xs">只记在这个浏览器里</div>
      </Field>
    </Section>
  );
}

export default function System() {
  const query = useQuery({ queryKey: ["system"], queryFn: api.system, refetchInterval: 15_000 });
  // 重启的那十几秒里取不到新的：手里有上一次的就接着显示（页面顶上有提示条说明数据不是最新的）
  const info = query.data;

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="系统信息"
        description="机器人进程现在的情况：跑了多久、连着什么、配置放在哪、凭据填没填。"
      />
      {info ? (
        // 每行两张卡片一样高；窄屏上排成一列
        <div className="grid gap-3 xl:grid-cols-2">
          <Running info={info} />
          <Connections status={info.status} />
          <Configuration info={info} />
          <Secrets secrets={info.secrets} />
          <Tables info={info} />
          <div className="flex flex-col gap-3">
            {info.status.backup ? <Backup backup={info.status.backup} /> : null}
            <LoginToken source={info.environment.token_source} />
            <Operations />
          </div>
        </div>
      ) : query.isPending ? (
        <div className="grid gap-3 xl:grid-cols-2">
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} className="h-56" />
          ))}
        </div>
      ) : (
        <>
          <Card>
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          </Card>
          <div className="max-w-xl">
            <Operations />
          </div>
        </>
      )}
    </div>
  );
}
