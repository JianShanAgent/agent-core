// 页面最上面的提示条：配置有改动没生效、覆写出了问题、连不上机器人时出现。
// 「重新加载」和「重启机器人」两个动作也放在这里，别的页面要用就调 useSystemActions。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Info, Power, RefreshCw, TriangleAlert, WifiOff } from "lucide-react";
import { type ReactNode, useCallback, useState, useSyncExternalStore } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { formatAgo, formatDateTime } from "@/lib/format";
import { useRefreshConfig, useStatus } from "@/lib/hooks";
import type { BackupState } from "@/lib/types";
import { cn } from "@/lib/utils";

type BannerTone = "danger" | "warning" | "info";

const TONES: Record<BannerTone, { box: string; icon: typeof Info }> = {
  danger: { box: "border-destructive/30 bg-destructive/8 text-destructive", icon: CircleAlert },
  warning: {
    box: "border-warning/40 bg-warning/12 text-[color-mix(in_oklch,var(--warning),var(--foreground)_45%)]",
    icon: TriangleAlert,
  },
  info: { box: "border-info/30 bg-info/10 text-info", icon: Info },
};

export function Banner({
  tone,
  icon,
  children,
  action,
  className,
}: {
  tone: BannerTone;
  icon?: typeof Info;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const Icon = icon ?? TONES[tone].icon;
  return (
    <div
      className={cn(
        "mb-4 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border px-3.5 py-2.5 text-sm",
        TONES[tone].box,
        className,
      )}
    >
      <Icon className="size-4 shrink-0" />
      <div className="text-foreground/90 min-w-0 flex-1 leading-relaxed">{children}</div>
      {action}
    </div>
  );
}

// 「正在重启」是整个页面共用的状态：从系统信息页点的重启，顶上的提示条也要知道
let restartingNow = false;
const restartListeners = new Set<() => void>();
function setRestarting(value: boolean): void {
  restartingNow = value;
  for (const notify of [...restartListeners]) notify();
}
function subscribeRestart(notify: () => void): () => void {
  restartListeners.add(notify);
  return () => restartListeners.delete(notify);
}

/** 重新加载（不重启，按盘上的配置重新装一套）和重启机器人。 */
export function useSystemActions() {
  const refresh = useRefreshConfig();
  const client = useQueryClient();
  const restarting = useSyncExternalStore(subscribeRestart, () => restartingNow);
  const [confirming, setConfirming] = useState(false);

  const reload = useMutation({
    mutationFn: api.reload,
    onSuccess: () => {
      toast.success("已按现在的配置重新加载");
      refresh();
    },
    onError: (error: Error) => {
      toast.error("重新加载没成，机器人仍按原来的配置运行", { description: error.message });
      refresh();
    },
  });

  // trigger：让机器人重启的那个请求（默认就是重启；更新、还原也是做完自己的事再重启）。
  // done：起来以后说的话，null = 不说（调用的一方自己看结果）。返回它起来了没有。
  const restart = useCallback(
    async (
      trigger: () => Promise<unknown> = api.restart,
      done: string | null = "机器人已重启",
      failed = "没能重启",
    ): Promise<boolean> => {
    setConfirming(false);
    setRestarting(true);
    const before = client.getQueryData<{ started_at: number }>(["status"])?.started_at;
    try {
      await trigger();
    } catch (error) {
      setRestarting(false);
      toast.error(failed, { description: (error as Error).message });
      return false;
    }
    // 等它起来：启动时间变了就是新的进程
    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
      try {
        const status = await api.status();
        if (status.started_at !== before) {
          setRestarting(false);
          if (done) toast.success(done);
          void client.invalidateQueries();
          return true;
        }
      } catch {
        /* 还没起来，接着等 */
      }
    }
    setRestarting(false);
    toast.error("等了一分半钟机器人还没起来，去看看日志");
    return false;
    },
    [client],
  );

  const confirmDialog = (
    <ConfirmDialog
      open={confirming}
      onOpenChange={setConfirming}
      title="重启机器人？"
      description={
        <>
          进程会原地重新启动，大约十几秒。正在处理的消息会中断；这段时间里的新消息由平台在重连后补发
          （一小时内）。
        </>
      }
      confirmText="重启"
      destructive
      onConfirm={() => void restart()}
    />
  );
  return {
    reload: () => reload.mutate(),
    reloading: reload.isPending,
    askRestart: () => setConfirming(true),
    restartWith: restart,
    restarting,
    confirmDialog,
  };
}

/** 太久没有成功的数据库备份了。备份在机器人之外的容器里做，出了问题只有这里看得到。 */
function BackupBanner({ backup }: { backup: BackupState }) {
  const last = backup.last;
  return (
    <Banner
      tone="danger"
      action={
        <Link to="/system" className="text-sm font-medium underline-offset-2 hover:underline">
          看详情
        </Link>
      }
    >
      <b>已经超过 {backup.warn_hours} 小时没有成功的数据库备份了。</b>{" "}
      {backup.last_ok_at
        ? `最近一次成功是 ${formatDateTime(backup.last_ok_at)}（${formatAgo(backup.last_ok_at)}）。`
        : "到现在一次成功的都没有。"}
      {last && !last.ok
        ? `最近一次（${formatDateTime(last.finished_at)}）没成：${last.detail || "没有记下原因"}。`
        : last
          ? ""
          : "备份容器没有留下任何记录，它可能没在运行。"}
      <div className="mt-0.5 text-xs opacity-90">
        到服务器上看备份容器的状态和日志：
        <code className="font-mono">ctl ps backup</code>、
        <code className="font-mono">ctl logs --tail 50 backup</code>
      </div>
    </Banner>
  );
}

export function StateBanners() {
  const { data: status, isError } = useStatus();
  const actions = useSystemActions();
  if (actions.restarting) {
    return (
      <>
        <Banner tone="info" icon={Power}>
          正在重启机器人，稍等十几秒……
        </Banner>
        {actions.confirmDialog}
      </>
    );
  }
  if (isError || !status) {
    return isError ? (
      <Banner tone="danger" icon={WifiOff}>
        连不上机器人：它可能正在重启，或者已经停了。页面上的数据不是最新的。
      </Banner>
    ) : null;
  }
  const state = status.config;
  const reloadButton = (
    <Button variant="outline" size="sm" loading={actions.reloading} onClick={actions.reload}>
      <RefreshCw /> 重新加载
    </Button>
  );
  const errors = Object.entries(state.override_errors);
  return (
    <>
      {status.backup?.overdue ? <BackupBanner backup={status.backup} /> : null}
      {state.overrides_ignored ? (
        <Banner tone="danger" action={reloadButton}>
          <b>网页上改的配置有问题，启动时被整个忽略了，机器人现在按默认值运行。</b>
          <div className="mt-0.5 font-mono text-xs break-all opacity-90">
            {state.overrides_ignored}
          </div>
          去「机器人配置」里把出问题的那一项改回来，再点重新加载。
        </Banner>
      ) : null}
      {errors.length ? (
        <Banner tone="warning">
          有 {errors.length} 个覆写文件没用上：
          {errors.map(([name, reason]) => (
            <span key={name} className="ml-1">
              <code className="font-mono text-xs">{name}</code>（{reason}）
            </span>
          ))}
        </Banner>
      ) : null}
      {state.pending_restart.length ? (
        <Banner
          tone="warning"
          action={
            <Button variant="outline" size="sm" onClick={actions.askRestart}>
              <Power /> 重启机器人
            </Button>
          }
        >
          这些配置改过了，要重启机器人才生效：
          <span className="font-mono text-xs">{state.pending_restart.join("、")}</span>
        </Banner>
      ) : null}
      {state.pending_reload.length && !state.overrides_ignored ? (
        <Banner tone="info" action={reloadButton}>
          盘上的配置和正在用的不一样（
          <span className="font-mono text-xs">{state.pending_reload.join("、")}</span>
          ），重新加载后生效。
        </Banner>
      ) : null}
      {actions.confirmDialog}
    </>
  );
}
