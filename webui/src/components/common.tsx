// 各个页面共用的小部件：页头、空状态、出错、加载中、数字卡片、Markdown、复制按钮。

import { Check, CircleAlert, Copy, DatabaseZap, Inbox, RefreshCw } from "lucide-react";
import { type ComponentType, type ReactNode, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError } from "@/lib/api";
import type { Tone } from "@/lib/labels";
import { cn, copyText } from "@/lib/utils";

import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Hint, Skeleton } from "./ui/misc";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description ? (
          <p className="text-muted-foreground mt-1 max-w-3xl text-sm">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function EmptyState({
  icon: Icon = Inbox,
  title,
  hint,
  action,
  className,
}: {
  icon?: ComponentType<{ className?: string }>;
  title: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "text-muted-foreground flex flex-col items-center justify-center gap-2 px-6 py-12 text-center",
        className,
      )}
    >
      <Icon className="size-8 opacity-40" />
      <div className="text-foreground/80 text-sm font-medium">{title}</div>
      {hint ? <div className="max-w-md text-xs leading-relaxed">{hint}</div> : null}
      {action}
    </div>
  );
}

/** 取数据出错。没有数据库时（接口回 503）换一种说法：那不是故障。 */
export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const message = error instanceof Error ? error.message : String(error);
  if (error instanceof ApiError && error.status === 503) {
    return <EmptyState icon={DatabaseZap} title="这一页现在用不了" hint={message} className={className} />;
  }
  return (
    <EmptyState
      icon={CircleAlert}
      title="没取到数据"
      hint={message}
      className={className}
      action={
        onRetry ? (
          <Button variant="outline" size="sm" onClick={onRetry} className="mt-1">
            <RefreshCw /> 再试一次
          </Button>
        ) : null
      }
    />
  );
}

export function LoadingRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2 p-4", className)}>
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-9 w-full" style={{ opacity: 1 - index * 0.14 }} />
      ))}
    </div>
  );
}

/** 首页那种一个数字一张卡。 */
export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  tone,
  onClick,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  tone?: "danger" | "warning";
  onClick?: () => void;
}) {
  return (
    <div
      className={cn(
        "bg-card rounded-xl border p-4 shadow-xs",
        onClick && "hover:border-foreground/25 cursor-pointer transition-colors",
      )}
      onClick={onClick}
    >
      <div className="text-muted-foreground flex items-center justify-between gap-2 text-xs">
        <span>{label}</span>
        {Icon ? <Icon className="size-4 opacity-60" /> : null}
      </div>
      <div
        className={cn(
          "mt-1.5 text-2xl font-semibold tracking-tight tabular-nums",
          tone === "danger" && "text-destructive",
          tone === "warning" && "text-warning",
        )}
      >
        {value}
      </div>
      {hint ? <div className="text-muted-foreground mt-1 text-xs">{hint}</div> : null}
    </div>
  );
}

const TONE_TO_BADGE: Record<Tone, "default" | "success" | "warning" | "danger" | "info" | "muted"> =
  {
    default: "default",
    success: "success",
    warning: "warning",
    danger: "danger",
    info: "info",
    muted: "muted",
  };

export function ToneBadge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <Badge tone={TONE_TO_BADGE[tone]}>{children}</Badge>;
}

/** 机器人的回复按 Markdown 显示（链接在新窗口打开）。 */
export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("prose-chat", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children: text, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {text}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

export function CopyButton({ text, label = "复制" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Hint text={done ? "已复制" : label}>
      <Button
        variant="ghost"
        size="icon-xs"
        className="text-muted-foreground"
        onClick={(event) => {
          event.stopPropagation();
          void copyText(text).then(() => {
            setDone(true);
            window.setTimeout(() => setDone(false), 1200);
          });
        }}
      >
        {done ? <Check className="text-success" /> : <Copy />}
        <span className="sr-only">{label}</span>
      </Button>
    </Hint>
  );
}

/** 「名字：值」的一行。 */
export function Field({
  label,
  children,
  className,
}: {
  label: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 items-baseline gap-3 text-sm", className)}>
      <span className="text-muted-foreground w-24 shrink-0 text-xs">{label}</span>
      <span className="min-w-0 flex-1 break-words">{children}</span>
    </div>
  );
}

/** 每页几条的翻页。 */
export function Pager({
  offset,
  limit,
  total,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  return (
    <div className="text-muted-foreground flex items-center justify-end gap-2 px-3 py-2 text-xs">
      <span>
        第 {page} / {pages} 页，共 {total.toLocaleString("en-US")} 条
      </span>
      <Button
        variant="outline"
        size="xs"
        disabled={page <= 1}
        onClick={() => onChange(Math.max(0, offset - limit))}
      >
        上一页
      </Button>
      <Button
        variant="outline"
        size="xs"
        disabled={page >= pages}
        onClick={() => onChange(offset + limit)}
      >
        下一页
      </Button>
    </div>
  );
}
