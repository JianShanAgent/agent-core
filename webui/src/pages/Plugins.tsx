// 插件：机器人的能力都是插件。这一页看每个插件注册了什么，启用、停用，调加载顺序。
// 拨开关、挪顺序都是马上保存：后端不重启进程就把插件重新装一遍，装不起来会撤回改动（接口回 409）。

import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, ChevronRight, Puzzle, RotateCcw, Settings } from "lucide-react";
import { Fragment, type ReactNode, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Banner } from "@/components/layout/StateBanners";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/form";
import { Hint, Pre, Skeleton, Spinner } from "@/components/ui/misc";
import { api, ApiError } from "@/lib/api";
import { useRefreshConfig } from "@/lib/hooks";
import type { PluginInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 一次改动：要存的启用列表，和存成之后怎么跟用户说。 */
interface Change {
  /** 完整的、按加载顺序排好的启用列表 */
  enabled: string[];
  /** 做成了什么，比如「已停用 memory」 */
  done: string;
  /** 动的是哪个插件（它的卡片上转圈）；恢复默认是 null */
  plugin: string | null;
}

/** 做之前要先问一句的改动。 */
interface Ask {
  title: string;
  description: ReactNode;
  confirmText: string;
  destructive: boolean;
  change: Change;
}

/** 停用之前要先问一句的插件，和停了以后的后果。 */
const STOP_WARNINGS: Record<string, string> = {
  chat:
    "chat 是对话入口。停了以后，命令以外的消息机器人都不再回答：" +
    "群里 @ 它闲聊、问游戏问题没有回应，私聊只回一句可用命令的提示。",
  safety:
    "safety 是出站过滤：每条回复发出去之前都要过它（去掉控制字符、拦下命中屏蔽词的、截短太长的）。" +
    "停了以后，回复不再过滤，原样发出去。",
};

const IDLE_REASON =
  "它加载了，但什么都没注册。常见原因：没有数据库；它依赖的别的插件没启用，或者排在它后面；" +
  "它自己的配置里 enabled 是 false。";

interface Standing {
  tone: "success" | "warning" | "danger" | "muted";
  text: string;
  /** 为什么是这个状态（鼠标停在标签上、展开卡片都能看到） */
  why?: string;
}

function standing(plugin: PluginInfo): Standing {
  if (!plugin.exists) {
    return {
      tone: "danger",
      text: "代码里没有这个插件",
      why: "启用列表里写了它，但代码里找不到。有它在，插件装配不起来，别的改动也存不上；把它停用掉。",
    };
  }
  // 下面两种是盘上的配置和正在运行的那一套对不上（手改了覆写文件还没重新加载，或者覆写在启动时被忽略了）
  if (plugin.enabled && !plugin.loaded) {
    return {
      tone: "warning",
      text: "启用了但还没加载",
      why: "配置里启用了它，正在运行的这一套里却没有：配置改过还没重新加载，或者覆写在启动时被忽略了。看页面最上面的提示。",
    };
  }
  if (!plugin.enabled) {
    return plugin.loaded
      ? {
          tone: "warning",
          text: "停用了但还在运行",
          why: "配置里已经停用，正在运行的这一套里还有它，重新加载之后才会停。",
        }
      : { tone: "muted", text: "没启用" };
  }
  if (!plugin.active) return { tone: "warning", text: "加载了但没起作用", why: IDLE_REASON };
  return { tone: "success", text: "运行中" };
}

const sameList = (a: string[], b: string[]) =>
  a.length === b.length && a.every((item, index) => item === b[index]);

/**
 * 启用一个插件时把它插在哪：默认顺序里排在它前面、现在启用着的最近那一个的后面；
 * 前面的都没启用就放最前；默认列表里没有它就放最后。
 */
function withEnabled(enabled: string[], defaults: string[], id: string): string[] {
  const at = defaults.indexOf(id);
  if (at < 0) return [...enabled, id];
  for (let index = at - 1; index >= 0; index--) {
    const after = enabled.indexOf(defaults[index]);
    if (after >= 0) return [...enabled.slice(0, after + 1), id, ...enabled.slice(after + 1)];
  }
  return [id, ...enabled];
}

const slash = (name: string) => `/${name.replace(/^\/+/, "")}`;

/** 收起时的一行小字：它注册了几个什么。 */
function outline(plugin: PluginInfo): string[] {
  const { hooks } = plugin;
  const parts: string[] = [];
  if (plugin.commands.length) parts.push(`${plugin.commands.length} 个命令`);
  if (plugin.tools.length) parts.push(`${plugin.tools.length} 个工具`);
  if (plugin.services.length) parts.push(`${plugin.services.length} 个服务`);
  if (plugin.keywords.length) parts.push(`${plugin.keywords.length} 个关键词`);
  if (hooks.chat) parts.push("接管对话入口");
  if (hooks.inbound) parts.push(`${hooks.inbound} 个入站中间件`);
  if (hooks.outbound) parts.push(`${hooks.outbound} 个出站过滤`);
  if (hooks.observers) parts.push(`${hooks.observers} 个旁听`);
  return parts;
}

/** 它挂在消息处理的哪些环节上（只列有的）。 */
function hookLines(hooks: PluginInfo["hooks"]): { name: string; note: string }[] {
  const lines: { name: string; note: string }[] = [];
  if (hooks.chat) lines.push({ name: "接管对话入口", note: "没命中命令和关键词的消息都交给它" });
  if (hooks.inbound) {
    lines.push({ name: `入站中间件 ${hooks.inbound} 个`, note: "消息进来先过它，它可以把消息拦下" });
  }
  if (hooks.outbound) {
    lines.push({
      name: `出站过滤 ${hooks.outbound} 个`,
      note: "回复发出去之前过它，它可以改、也可以拦下",
    });
  }
  if (hooks.observers) {
    lines.push({
      name: `旁听 ${hooks.observers} 个`,
      note: "收到的每条消息都给它看一眼，包括不是对机器人说的",
    });
  }
  return lines;
}

/** 工具的参数名（JSON Schema 的 properties 里的键）。 */
function parameterNames(schema: unknown): string[] {
  const properties = (schema as { properties?: unknown } | null)?.properties;
  return properties !== null && typeof properties === "object" ? Object.keys(properties) : [];
}

function Section({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="text-muted-foreground mb-1.5 text-xs">
        <span className="font-medium">{title}</span>
        {note ? <span className="opacity-80">（{note}）</span> : null}
      </div>
      {children}
    </section>
  );
}

function ToolRow({ tool }: { tool: PluginInfo["tools"][number] }) {
  const [open, setOpen] = useState(false);
  const names = parameterNames(tool.parameters);
  return (
    <div className="px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <code className="font-mono text-[13px] font-medium">{tool.name}</code>
        <span className="text-muted-foreground text-xs">
          {names.length ? (
            <>
              参数 <span className="font-mono">{names.join("、")}</span>
            </>
          ) : (
            "不带参数"
          )}
        </span>
        <Button
          variant="ghost"
          size="xs"
          className="text-muted-foreground ml-auto"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
          参数的 JSON Schema
        </Button>
      </div>
      {tool.description ? (
        <div className="text-foreground/80 mt-0.5 text-[13px] leading-relaxed break-words whitespace-pre-wrap">
          {tool.description}
        </div>
      ) : null}
      {open ? <Pre className="mt-2">{JSON.stringify(tool.parameters ?? {}, null, 2)}</Pre> : null}
    </div>
  );
}

/** 卡片展开后的详情。哪一类是空的就不显示哪一类，所以没启用的插件只有说明。 */
function Details({ plugin, status }: { plugin: PluginInfo; status: Standing }) {
  const hooks = hookLines(plugin.hooks);
  return (
    <div className="flex flex-col gap-4 border-t px-4 py-4 sm:pl-[5.25rem]">
      {status.why ? (
        <div
          className={cn(
            "rounded-md border px-3 py-2 text-xs leading-relaxed",
            status.tone === "danger"
              ? "border-destructive/30 bg-destructive/8"
              : "border-warning/40 bg-warning/8",
          )}
        >
          {status.why}
        </div>
      ) : null}
      {plugin.exists ? (
        <Section title="说明">
          {plugin.description ? (
            <div className="text-sm leading-relaxed break-words whitespace-pre-wrap">
              {plugin.description}
            </div>
          ) : (
            <div className="text-muted-foreground text-sm">代码里没有给它写说明。</div>
          )}
        </Section>
      ) : null}
      {plugin.commands.length ? (
        <Section title="命令">
          <div className="flex flex-col divide-y rounded-md border">
            {plugin.commands.map((command) => (
              <div
                key={command.name}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-[13px]"
              >
                <code className="font-mono font-medium">{slash(command.name)}</code>
                {command.aliases.length ? (
                  <span className="text-muted-foreground text-xs">
                    别名 <span className="font-mono">{command.aliases.map(slash).join("、")}</span>
                  </span>
                ) : null}
                <span className="text-foreground/80 min-w-0 break-words">
                  {command.description || "没有写说明"}
                </span>
              </div>
            ))}
          </div>
        </Section>
      ) : null}
      {plugin.tools.length ? (
        <Section title="给模型用的工具" note="对话时模型自己决定要不要调用">
          <div className="flex flex-col divide-y rounded-md border">
            {plugin.tools.map((tool) => (
              <ToolRow key={tool.name} tool={tool} />
            ))}
          </div>
        </Section>
      ) : null}
      {plugin.services.length ? (
        <Section title="注册的服务" note="排在它后面的插件按名字取用">
          <div className="flex flex-wrap gap-1.5">
            {plugin.services.map((name) => (
              <Badge key={name} tone="outline" className="font-mono font-normal">
                {name}
              </Badge>
            ))}
          </div>
        </Section>
      ) : null}
      {hooks.length ? (
        <Section title="挂在哪些环节上">
          <ul className="flex flex-col gap-1 text-[13px]">
            {hooks.map((hook) => (
              <li key={hook.name} className="flex flex-wrap items-baseline gap-x-2.5">
                <span className="font-medium">{hook.name}</span>
                <span className="text-muted-foreground text-xs">{hook.note}</span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      {plugin.keywords.length ? (
        <Section title="关键词正则" note="消息匹配上其中一条就交给它处理，不用打命令">
          <Pre>{plugin.keywords.join("\n")}</Pre>
        </Section>
      ) : null}
    </div>
  );
}

function PluginCard({
  plugin,
  order,
  canUp,
  canDown,
  locked,
  busy,
  checked,
  onToggle,
  onMove,
}: {
  plugin: PluginInfo;
  /** 第几个加载（从 1 数）；没启用是 null */
  order: number | null;
  canUp: boolean;
  canDown: boolean;
  /** 改不了：没有覆写目录，或者正在保存 */
  locked: boolean;
  /** 正在保存的就是它 */
  busy: boolean;
  /** 开关的位置：保存期间是用户拨到的位置，其余时候是真实状态 */
  checked: boolean;
  onToggle: (on: boolean) => void;
  onMove: (step: -1 | 1) => void;
}) {
  const [open, setOpen] = useState(false);
  const status = standing(plugin);
  const parts = outline(plugin);
  const brief = parts.length ? parts.join(" · ") : plugin.loaded ? "什么都没注册" : "";
  return (
    <Card>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2 p-4">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="focus-visible:ring-ring/50 flex min-w-0 flex-1 basis-64 cursor-pointer items-start gap-3 rounded-md text-left outline-none focus-visible:ring-2"
        >
          <ChevronRight
            className={cn(
              "text-muted-foreground mt-1.5 size-4 shrink-0 transition-transform",
              open && "rotate-90",
            )}
          />
          <span
            title={order === null ? undefined : `第 ${order} 个加载`}
            className={cn(
              "flex size-7 shrink-0 items-center justify-center rounded-md font-mono text-xs tabular-nums",
              order !== null && "bg-muted text-muted-foreground",
            )}
          >
            {order}
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex min-h-7 flex-wrap items-center gap-x-2 gap-y-1">
              <span
                className={cn(
                  "font-mono text-sm font-semibold",
                  !plugin.enabled && "text-muted-foreground",
                )}
              >
                {plugin.id}
              </span>
              <Hint text={status.why}>
                <Badge tone={status.tone}>{status.text}</Badge>
              </Hint>
            </span>
            <span className="text-muted-foreground mt-0.5 block text-sm leading-relaxed">
              {plugin.exists
                ? plugin.summary || "代码里没有给它写说明"
                : "启用列表里有它，代码里找不到"}
            </span>
            {!open && brief ? (
              <span className="text-muted-foreground/80 mt-1 block text-xs">{brief}</span>
            ) : null}
          </span>
        </button>
        <div className="ml-auto flex h-7 shrink-0 items-center gap-1">
          {/* 转圈的位置一直留着：保存开始、结束时旁边的东西不跳 */}
          <span className="flex size-4 items-center justify-center">
            {busy ? <Spinner /> : null}
          </span>
          {plugin.config ? (
            <Button asChild variant="ghost" size="xs" className="text-muted-foreground">
              <Link to={`/config/${plugin.config}`}>
                <Settings className="size-3.5" /> 改它的配置
              </Link>
            </Button>
          ) : null}
          {order !== null ? (
            <>
              <Hint text="上移：早一点加载">
                <Button
                  variant="ghost"
                  size="icon-xs"
                  disabled={locked || !canUp}
                  onClick={() => onMove(-1)}
                >
                  <ArrowUp />
                  <span className="sr-only">上移 {plugin.id}</span>
                </Button>
              </Hint>
              <Hint text="下移：晚一点加载">
                <Button
                  variant="ghost"
                  size="icon-xs"
                  disabled={locked || !canDown}
                  onClick={() => onMove(1)}
                >
                  <ArrowDown />
                  <span className="sr-only">下移 {plugin.id}</span>
                </Button>
              </Hint>
            </>
          ) : null}
          <Switch
            className="ml-1.5"
            checked={checked}
            disabled={locked}
            onCheckedChange={onToggle}
            aria-label={`${plugin.enabled ? "停用" : "启用"} ${plugin.id}`}
          />
        </div>
      </div>
      {open ? <Details plugin={plugin} status={status} /> : null}
    </Card>
  );
}

export default function Plugins() {
  const refresh = useRefreshConfig();
  const query = useQuery({ queryKey: ["plugins"], queryFn: api.plugins });
  const [asking, setAsking] = useState<Ask | null>(null);

  const save = useMutation({
    mutationFn: (change: Change) => api.savePlugins(change.enabled),
    onSuccess: (_result, change) => {
      toast.success(`${change.done}，插件重新装配好了`);
    },
    onError: (error) => {
      // 请求没发到、或者回应没收到：改没改成不知道，不能说「仍按原来的运行」
      const lost = error instanceof ApiError && error.status === 0;
      toast.error(lost ? "没连上机器人，不确定改成了没有" : "没改成，机器人仍按原来的运行", {
        description: error.message,
      });
    },
    // 成不成都重取。等这一页的数据回来才算保存结束：开关在这之前停在用户拨的位置，
    // 之后直接落到真实状态，不会先弹回旧位置再跳一次。
    onSettled: async () => {
      refresh();
      // refresh 已经发起了这一页的重取，这里只是等它，不另发一次
      await query.refetch({ cancelRefetch: false });
    },
  });

  const data = query.data;
  const saving = save.isPending;
  const pending = saving ? save.variables : undefined;
  const locked = saving || !data?.state.writable;

  const toggle = (plugin: PluginInfo, on: boolean) => {
    if (!data) return;
    if (on) {
      save.mutate({
        enabled: withEnabled(data.enabled, data.default, plugin.id),
        done: `已启用 ${plugin.id}`,
        plugin: plugin.id,
      });
      return;
    }
    const change: Change = {
      enabled: data.enabled.filter((id) => id !== plugin.id),
      done: `已停用 ${plugin.id}`,
      plugin: plugin.id,
    };
    const warning = STOP_WARNINGS[plugin.id];
    if (warning) {
      setAsking({
        title: `停用 ${plugin.id}？`,
        description: warning,
        confirmText: "停用",
        destructive: true,
        change,
      });
    } else {
      save.mutate(change);
    }
  };

  const move = (plugin: PluginInfo, step: -1 | 1) => {
    if (!data) return;
    const from = data.enabled.indexOf(plugin.id);
    const to = from + step;
    if (from < 0 || to < 0 || to >= data.enabled.length) return;
    const enabled = [...data.enabled];
    const other = enabled[to];
    enabled[to] = plugin.id;
    enabled[from] = other;
    save.mutate({
      enabled,
      done: `已把 ${plugin.id} 挪到 ${other} ${step < 0 ? "前面" : "后面"}`,
      plugin: plugin.id,
    });
  };

  const askRestore = () => {
    if (!data) return;
    const added = data.default.filter((id) => !data.enabled.includes(id));
    const removed = data.enabled.filter((id) => !data.default.includes(id));
    setAsking({
      title: "恢复默认的插件列表？",
      description: (
        <div className="flex flex-col gap-1.5">
          <div>启用哪些插件、按什么顺序加载，都变回发布时的默认，马上重新装配。</div>
          <div>
            默认是：
            <span className="text-foreground font-mono text-xs">{data.default.join(" → ")}</span>
          </div>
          {added.length ? (
            <div>
              会启用：<span className="text-foreground font-mono text-xs">{added.join("、")}</span>
            </div>
          ) : null}
          {removed.length ? (
            <div>
              会停用：
              <span className="text-foreground font-mono text-xs">{removed.join("、")}</span>
            </div>
          ) : null}
          {added.length || removed.length ? null : <div>启用的插件不变，只是顺序变回默认。</div>}
        </div>
      ),
      confirmText: "恢复默认",
      destructive: false,
      change: { enabled: data.default, done: "已恢复默认的插件列表", plugin: null },
    });
  };

  const missing = data ? data.plugins.filter((plugin) => !plugin.exists).map((p) => p.id) : [];

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="插件"
        description="机器人的能力都是插件。开关一拨就生效，不用重启。"
        actions={
          data && !sameList(data.enabled, data.default) ? (
            <Button
              variant="outline"
              size="sm"
              disabled={locked}
              loading={saving && pending?.plugin === null}
              onClick={askRestore}
            >
              {saving && pending?.plugin === null ? null : <RotateCcw />} 恢复默认
            </Button>
          ) : null
        }
      />

      {query.isPending ? (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 7 }, (_, index) => (
            <Skeleton key={index} className="h-[4.75rem] rounded-xl" />
          ))}
        </div>
      ) : query.isError || !data ? (
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      ) : (
        <>
          {data.state.writable ? null : (
            <Banner tone="info" className="mb-0">
              没有配置覆写目录（环境变量{" "}
              <code className="font-mono text-xs">JIANSHANAGENT_CONFIG_OVERRIDES_DIR</code>{" "}
              没设），这里只能看不能改。
            </Banner>
          )}
          {missing.length ? (
            <Banner tone="warning" className="mb-0">
              启用列表里有代码里没有的插件：
              <span className="font-mono text-xs">{missing.join("、")}</span>
              。先把它停用，别的改动才存得上。
            </Banner>
          ) : null}

          {data.plugins.length === 0 ? (
            <Card>
              <EmptyState
                icon={Puzzle}
                title="一个插件都没有"
                hint="代码里没有找到任何插件，启用列表也是空的。机器人现在只认内核自带的那几条命令。"
              />
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {data.plugins.map((plugin, index) => {
                const at = data.enabled.indexOf(plugin.id);
                const firstOff = at < 0 && (index === 0 || data.plugins[index - 1].enabled);
                return (
                  <Fragment key={plugin.id}>
                    {firstOff ? (
                      <div className="text-muted-foreground mt-2 px-1 text-xs">
                        代码里有、现在没启用的
                      </div>
                    ) : null}
                    <PluginCard
                      plugin={plugin}
                      order={at < 0 ? null : at + 1}
                      canUp={at > 0}
                      canDown={at >= 0 && at < data.enabled.length - 1}
                      locked={locked}
                      busy={saving && pending?.plugin === plugin.id}
                      checked={pending ? pending.enabled.includes(plugin.id) : plugin.enabled}
                      onToggle={(on) => toggle(plugin, on)}
                      onMove={(step) => move(plugin, step)}
                    />
                  </Fragment>
                );
              })}
            </div>
          )}

          <p className="text-muted-foreground mt-1 max-w-4xl px-1 text-xs leading-relaxed">
            插件按从上到下的顺序加载，后加载的可以用先加载的注册的服务，所以{" "}
            <code className="font-mono">research</code> 要排在{" "}
            <code className="font-mono">knowledge</code> 和{" "}
            <code className="font-mono">wiki_query</code> 后面，
            <code className="font-mono">chat</code> 要排在{" "}
            <code className="font-mono">memory</code> 和{" "}
            <code className="font-mono">research</code> 后面。启用哪些、按什么顺序，写在{" "}
            <code className="font-mono">core.plugins</code>{" "}
            的覆写里，发布新版本不会被盖掉；以后的版本新加的默认插件会自动启用，在这里关掉的不会被重新打开。
          </p>
        </>
      )}

      <ConfirmDialog
        open={asking !== null}
        onOpenChange={(open) => {
          if (!open) setAsking(null);
        }}
        title={asking?.title ?? ""}
        description={asking?.description}
        confirmText={asking?.confirmText}
        destructive={asking?.destructive}
        onConfirm={() => {
          if (!asking) return;
          setAsking(null);
          save.mutate(asking.change);
        }}
      />
    </div>
  );
}
