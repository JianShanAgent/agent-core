// 「用户与会话」「群聊记录」两页共用的小部件（搜索框知识库页也用）：
// 左右两栏、搜索框、改备注名、聊天记录里按天分隔。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pencil, Search, X } from "lucide-react";
import { type ReactNode, useState } from "react";
import { toast } from "sonner";

import { CopyButton } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/form";
import { api } from "@/lib/api";
import { parseTime, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";

// ---- 左右两栏 ----

/**
 * 左边列表、右边详情。宽屏（xl 起）两栏并排；再窄的话右边只剩四百来像素，
 * 聊天记录和表格都挤不下，所以改成一次只显示一栏：没选时是列表，选了以后是详情。
 */
export function TwoPane({
  selected,
  list,
  detail,
}: {
  selected: boolean;
  list: ReactNode;
  detail: ReactNode;
}) {
  return (
    <div className="flex min-h-0 flex-1 gap-3">
      <Card
        className={cn(
          "w-full shrink-0 flex-col overflow-hidden xl:flex xl:w-[22rem]",
          selected ? "hidden" : "flex",
        )}
      >
        {list}
      </Card>
      <Card
        className={cn("min-w-0 flex-1 flex-col overflow-hidden xl:flex", selected ? "flex" : "hidden")}
      >
        {detail}
      </Card>
    </div>
  );
}

// ---- 搜索框 ----

export function SearchBox({
  value,
  onChange,
  placeholder,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}) {
  return (
    <div className={cn("relative", className)}>
      <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
      <Input
        className="h-8 pr-8 pl-8"
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {value ? (
        <button
          type="button"
          onClick={() => onChange("")}
          className="text-muted-foreground hover:text-foreground absolute top-1/2 right-2 -translate-y-1/2 cursor-pointer rounded-sm p-0.5"
        >
          <X className="size-3.5" />
          <span className="sr-only">清空</span>
        </button>
      ) : null}
    </div>
  );
}

/**
 * 翻到第几页（从第几条开始）。它跟着筛选条件走：条件一变就回到第一页。
 * 不用 effect 去重置：那样条件变了以后会先按旧的页码多查一次。
 */
export function useOffset(filter: string): [number, (offset: number) => void] {
  const [page, setPage] = useState({ filter, offset: 0 });
  if (page.filter !== filter) setPage({ filter, offset: 0 });
  const offset = page.filter === filter ? page.offset : 0;
  return [offset, (next) => setPage({ filter, offset: next })];
}

// ---- 名字 ----

interface Named {
  scope: string;
  chat_id: string;
  user_id: string;
  chat_label: string;
  user_label: string;
  user_name?: string | null;
  channel?: string | null;
}

/** 这个会话是不是群里某个成员的（群里每个人各有一份）。 */
export const isMember = (row: Named): boolean => row.scope === "group" && row.user_id !== "";

/** 会话显示成什么名字。群成员：成员的名字；单聊：对方的名字。备注名 > 昵称 > 标识开头几位。 */
export function sessionName(row: Named): string {
  if (isMember(row)) return row.user_label || row.user_name || shortId(row.user_id);
  return row.chat_label || row.user_name || shortId(row.chat_id);
}

export const chatName = (row: { chat_id: string; chat_label: string }): string =>
  row.chat_label || shortId(row.chat_id);

/** 是不是从网页对话来的（网页上的身份以 web- 开头，和 QQ 上的用户互不相干）。 */
export const fromWeb = (row: Named): boolean =>
  row.channel === "web" || row.chat_id.startsWith("web-") || row.user_id.startsWith("web-");

// ---- 备注名 ----

export type LabelKind = "chat" | "user";

/** 改备注名。备注名在会话列表、群聊记录里到处显示，改完这两类查询都重取。 */
export function useSetLabel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { kind: LabelKind; id: string; label: string }) =>
      api.setLabel(input.kind, input.id, input.label),
    onSuccess: (saved) => {
      toast.success(saved.label ? `备注名改成「${saved.label}」了` : "备注名去掉了");
      void client.invalidateQueries({ queryKey: ["sessions"] });
      void client.invalidateQueries({ queryKey: ["chatlog"] });
    },
    onError: (error: Error) => {
      toast.error("备注名没改成", { description: error.message });
    },
  });
}

// 后端最多留 40 个字
const LABEL_MAX = 40;

/** 一行备注名：平时显示名字和「改」，点了以后变成一个小输入框。留空保存 = 去掉备注名。 */
export function InlineLabel({
  kind,
  id,
  value,
  name,
}: {
  kind: LabelKind;
  id: string;
  /** 现在的备注名；没起是空字符串 */
  value: string;
  /** 这是给谁起的名，比如「群的备注名」 */
  name: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const save = useSetLabel();
  const close = () => setDraft(null);

  if (draft === null) {
    return (
      <div className="flex min-w-0 items-center gap-1.5 text-sm">
        <span className="text-muted-foreground shrink-0 text-xs">{name}</span>
        {value ? (
          <span className="min-w-0 truncate font-medium">{value}</span>
        ) : (
          <span className="text-muted-foreground">没起</span>
        )}
        <Button
          variant="ghost"
          size="xs"
          className="text-muted-foreground"
          onClick={() => setDraft(value)}
        >
          <Pencil className="size-3" /> 改
        </Button>
      </div>
    );
  }
  return (
    <form
      className="flex flex-wrap items-center gap-1.5 text-sm"
      onSubmit={(event) => {
        event.preventDefault();
        const next = draft.trim();
        if (next === value) close();
        else save.mutate({ kind, id, label: next }, { onSuccess: close });
      }}
    >
      <span className="text-muted-foreground shrink-0 text-xs">{name}</span>
      <Input
        autoFocus
        className="h-7 w-44 px-2 text-[13px]"
        maxLength={LABEL_MAX}
        placeholder="留空 = 去掉备注名"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") close();
        }}
      />
      <Button type="submit" size="xs" loading={save.isPending}>
        保存
      </Button>
      <Button type="button" variant="ghost" size="xs" onClick={close} disabled={save.isPending}>
        取消
      </Button>
    </form>
  );
}

export interface LabelTarget {
  kind: LabelKind;
  id: string;
  /** 现在的备注名 */
  label: string;
  /** 平台给的昵称（有的话） */
  nickname?: string | null;
}

function LabelForm({ target, onClose }: { target: LabelTarget; onClose: () => void }) {
  const [draft, setDraft] = useState(target.label);
  const save = useSetLabel();
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        const next = draft.trim();
        if (next === target.label) onClose();
        else save.mutate({ kind: target.kind, id: target.id, label: next }, { onSuccess: onClose });
      }}
    >
      <DialogHeader>
        <DialogTitle>{target.kind === "user" ? "给这个人起备注名" : "给这个会话起备注名"}</DialogTitle>
        <DialogDescription>
          平台给的标识是一长串看不懂的字符。起了备注名，会话列表和群聊记录里就显示这个名字。留空保存 =
          去掉备注名。
        </DialogDescription>
      </DialogHeader>
      <div className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        {target.nickname ? <span>昵称 {target.nickname}</span> : null}
        <span className="inline-flex min-w-0 items-center gap-1">
          标识 <code className="font-mono break-all">{target.id}</code>
          <CopyButton text={target.id} label="复制标识" />
        </span>
      </div>
      <Input
        autoFocus
        maxLength={LABEL_MAX}
        placeholder="备注名"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={save.isPending}>
          取消
        </Button>
        <Button type="submit" loading={save.isPending}>
          保存
        </Button>
      </DialogFooter>
    </form>
  );
}

/** 起备注名的小对话框（群聊记录里点一个人的名字时用）。 */
export function LabelDialog({
  target,
  onClose,
}: {
  target: LabelTarget | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={target !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        {target ? (
          <LabelForm key={`${target.kind}:${target.id}`} target={target} onClose={onClose} />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

// ---- 聊天记录里的时间 ----

const pad = (value: number) => String(value).padStart(2, "0");
const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

/** 只有几点几分：跨天的地方另有日期分隔，每条不再带日期。 */
export function clock(value: string): string {
  const date = parseTime(value);
  return date ? `${pad(date.getHours())}:${pad(date.getMinutes())}` : "—";
}

/** 同一天的时间得到同一个值（用来判断两条消息之间有没有跨天）。 */
export function dayKey(value: string): string {
  return parseTime(value)?.toDateString() ?? "";
}

function dayLabel(value: string): string {
  const date = parseTime(value);
  if (!date) return "—";
  const now = new Date();
  const midnight = (moment: Date) =>
    new Date(moment.getFullYear(), moment.getMonth(), moment.getDate()).getTime();
  const days = Math.round((midnight(now) - midnight(date)) / 86_400_000);
  const year = date.getFullYear() === now.getFullYear() ? "" : `${date.getFullYear()} 年 `;
  const text = `${year}${date.getMonth() + 1} 月 ${date.getDate()} 日 星期${WEEKDAYS[date.getDay()]}`;
  if (days === 0) return `今天 · ${text}`;
  if (days === 1) return `昨天 · ${text}`;
  return text;
}

/** 跨天的地方插的一行日期。 */
export function DayDivider({ value, className }: { value: string; className?: string }) {
  return (
    <div className={cn("text-muted-foreground flex items-center gap-3 py-2 text-[11px]", className)}>
      <span className="bg-border h-px flex-1" />
      {dayLabel(value)}
      <span className="bg-border h-px flex-1" />
    </div>
  );
}
