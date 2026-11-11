// 配置表单里的一项：按类型给不同的控件，旁边显示说明（来自 YAML 里的注释）、
// 和默认值不一样时标出来并可以一键恢复。
// 不能在网页上改的项（后端在表单结构里标了 locked）只显示、不能动，旁边写明为什么。

import { ChevronDown, Lock, Plus, RotateCcw, Trash2, X } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Switch, Textarea } from "@/components/ui/form";
import { Hint } from "@/components/ui/misc";
import type { ConfigValues, SchemaField } from "@/lib/types";
import { cn, getAt, sameValue } from "@/lib/utils";

/** 默认值显示成一小段文字（鼠标停在「已改」上时看）。 */
export function showValue(value: unknown): string {
  if (value === undefined) return "（默认值里没有这一项）";
  if (typeof value === "string") return value === "" ? "（空）" : value;
  return JSON.stringify(value);
}

/** 数字输入：打字过程中允许空着、只打了负号这类中间状态，能算成数字时才往外交。 */
function NumberInput({
  value,
  integer,
  disabled,
  onChange,
}: {
  value: unknown;
  integer: boolean;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  const [text, setText] = useState(String(value ?? ""));
  useEffect(() => {
    // 外面的值变了（恢复默认、放弃改动）而且和框里的不是同一个数：以外面的为准
    if (Number(text) !== value) setText(String(value ?? ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const parsed = Number(text);
  const valid = text.trim() !== "" && Number.isFinite(parsed) && (!integer || Number.isInteger(parsed));
  return (
    <div className="w-36">
      <Input
        inputMode="decimal"
        className="h-8 text-right tabular-nums"
        value={text}
        disabled={disabled}
        aria-invalid={!valid}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          const number = Number(next);
          if (next.trim() !== "" && Number.isFinite(number) && (!integer || Number.isInteger(number))) {
            onChange(number);
          }
        }}
      />
      {valid ? null : (
        <div className="text-destructive mt-1 text-right text-[11px]">
          {integer ? "要填整数" : "要填数字"}
        </div>
      )}
    </div>
  );
}

/** 一串单个的值（工具名、关键字……）：一个一个的小标签，可以删、可以加。 */
function ListInput({
  value,
  numeric,
  disabled,
  onChange,
}: {
  value: unknown[];
  numeric: boolean;
  disabled: boolean;
  onChange: (value: unknown[]) => void;
}) {
  const [text, setText] = useState("");
  const add = () => {
    const piece = text.trim();
    if (!piece) return;
    if (numeric && !Number.isFinite(Number(piece))) return;
    onChange([...value, numeric ? Number(piece) : piece]);
    setText("");
  };
  const onKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      add();
    } else if (event.key === "Backspace" && !text && value.length) {
      onChange(value.slice(0, -1));
    }
  };
  return (
    <div
      className={cn(
        "border-input bg-card flex min-h-8 w-full flex-wrap items-center gap-1 rounded-md border px-1.5 py-1 shadow-xs",
        disabled && "opacity-50",
      )}
    >
      {value.map((item, index) => (
        <span
          key={`${String(item)}-${index}`}
          className="bg-secondary flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-xs"
        >
          {String(item)}
          {disabled ? null : (
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground cursor-pointer"
              onClick={() => onChange(value.filter((_, at) => at !== index))}
            >
              <X className="size-3" />
              <span className="sr-only">去掉</span>
            </button>
          )}
        </span>
      ))}
      {disabled ? null : (
        <input
          className="placeholder:text-muted-foreground/70 min-w-24 flex-1 bg-transparent px-1 text-sm outline-none"
          placeholder={value.length ? "再加一个，回车" : "填一个，回车"}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKey}
          onBlur={add}
        />
      )}
    </div>
  );
}

/** 结构复杂的值（列表里套着键值）：整个按 JSON 改，合法了才往外交。 */
function JsonInput({
  value,
  disabled,
  onChange,
}: {
  value: unknown;
  disabled: boolean;
  onChange: (value: unknown) => void;
}) {
  const pretty = JSON.stringify(value ?? null, null, 2);
  const [text, setText] = useState(pretty);
  const [error, setError] = useState("");
  useEffect(() => {
    try {
      if (sameValue(JSON.parse(text), value ?? null)) return;
    } catch {
      /* 框里的不合法：下面以外面的为准 */
    }
    setText(pretty);
    setError("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pretty]);
  return (
    <div className="w-full">
      <Textarea
        className="font-mono text-xs"
        rows={Math.min(16, Math.max(3, text.split("\n").length))}
        value={text}
        disabled={disabled}
        spellCheck={false}
        aria-invalid={Boolean(error)}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          try {
            onChange(JSON.parse(next));
            setError("");
          } catch (failure) {
            setError((failure as Error).message);
          }
        }}
      />
      {error ? (
        <div className="text-destructive mt-1 text-[11px]">不是合法的 JSON：{error}</div>
      ) : null}
    </div>
  );
}

/** 说明文字：长的先只显示两行，点一下展开。 */
function Description({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  const long = text.length > 110 || text.includes("\n");
  return (
    <div
      className={cn(
        "text-muted-foreground mt-1 text-xs leading-relaxed whitespace-pre-line",
        long && !open && "line-clamp-2 cursor-pointer",
        long && open && "cursor-pointer",
      )}
      onClick={() => long && setOpen(!open)}
    >
      {text}
    </div>
  );
}

/** 写在锁住的项旁边的那句话。 */
export const LOCKED_NOTE = "这一项不能在网页上改，要改仓库里的配置文件再发布";

/** 锁住的项下面的一行说明。它和默认值不一样时（手改过覆写文件）多说一句怎么办。 */
function LockedNote({ changed, added }: { changed: boolean; added: boolean }) {
  return (
    <div className="text-muted-foreground mt-1 flex items-start gap-1 text-xs leading-relaxed">
      <Lock className="mt-0.5 size-3 shrink-0" />
      <span>
        {LOCKED_NOTE}
        {changed
          ? `。它现在和默认值不一样，带着它这份配置存不上：先${added ? "把它去掉" : "恢复成默认值"}`
          : null}
      </span>
    </div>
  );
}

interface FieldProps {
  field: SchemaField;
  base: ConfigValues;
  draft: ConfigValues;
  disabled: boolean;
  /** 只显示和它（小写）对得上的项；空 = 全部显示 */
  filter: string;
  /** 只显示改过的 */
  changedOnly: boolean;
  /** 它上面那一组整个是锁住的：说明在那一组上写过了，这一项不再重复 */
  inLocked?: boolean;
  onChange: (path: string[], value: unknown) => void;
}

/** 这一项（或者它下面的某一项）符不符合筛选条件。 */
function visible(field: SchemaField, props: Omit<FieldProps, "field">): boolean {
  if (field.kind === "group") return (field.children ?? []).some((child) => visible(child, props));
  if (props.changedOnly && sameValue(getAt(props.base, field.path), getAt(props.draft, field.path))) {
    return false;
  }
  if (!props.filter) return true;
  return `${field.path.join(".")} ${field.description}`.toLowerCase().includes(props.filter);
}

function Control({
  field,
  value,
  disabled,
  onChange,
}: {
  field: SchemaField;
  value: unknown;
  disabled: boolean;
  onChange: (value: unknown) => void;
}): ReactNode {
  switch (field.kind) {
    case "bool":
      return <Switch checked={value === true} disabled={disabled} onCheckedChange={onChange} />;
    case "int":
    case "float":
      return (
        <NumberInput
          value={value}
          integer={field.kind === "int"}
          disabled={disabled}
          onChange={onChange}
        />
      );
    case "str":
      return (
        <Input
          className="h-8"
          value={typeof value === "string" ? value : String(value ?? "")}
          disabled={disabled}
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
        />
      );
    case "text": {
      const text = typeof value === "string" ? value : String(value ?? "");
      return (
        <Textarea
          rows={Math.min(14, Math.max(3, text.split("\n").length + 1))}
          value={text}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
      );
    }
    case "list":
      return Array.isArray(value) ? (
        <ListInput
          value={value}
          numeric={field.items === "number"}
          disabled={disabled}
          onChange={onChange}
        />
      ) : (
        <JsonInput value={value} disabled={disabled} onChange={onChange} />
      );
    default:
      return <JsonInput value={value} disabled={disabled} onChange={onChange} />;
  }
}

function Leaf({ field, base, draft, disabled, inLocked, onChange }: FieldProps) {
  const value = getAt(draft, field.path);
  const original = getAt(base, field.path);
  const removed = value === undefined;
  const changed = !sameValue(original, value);
  const locked = field.locked === true;
  // 开关和数字的控件窄，放右边；文字、列表、JSON 占一整行
  const inline = field.kind === "bool" || field.kind === "int" || field.kind === "float";
  const marks = (
    <>
      {changed ? (
        <Hint text={field.added ? "默认值里没有这一项，是后来加的" : `默认：${showValue(original)}`}>
          <Badge tone="brand">{field.added ? "加的" : removed ? "删了" : "已改"}</Badge>
        </Hint>
      ) : null}
      {changed && !disabled ? (
        <Hint text={field.added ? "去掉这一项" : "恢复成默认值"}>
          <Button
            variant="ghost"
            size="icon-xs"
            className="text-muted-foreground"
            onClick={() => onChange(field.path, original)}
          >
            {field.added ? <Trash2 /> : <RotateCcw />}
          </Button>
        </Hint>
      ) : null}
    </>
  );
  return (
    <div className={cn("py-3", changed && "bg-brand/[0.04] -mx-4 px-4")}>
      <div className={cn("flex gap-x-4 gap-y-2", inline ? "items-start" : "flex-col")}>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <code className="font-mono text-[13px] font-medium">{field.key}</code>
            {inline ? null : marks}
          </div>
          <Description text={field.description} />
          {locked && !inLocked ? <LockedNote changed={changed} added={field.added} /> : null}
        </div>
        {removed ? (
          <span className="text-muted-foreground text-xs">这一项被删掉了</span>
        ) : (
          <div className={cn("flex items-start gap-1.5", inline ? "shrink-0" : "w-full")}>
            {inline ? marks : null}
            {/* 锁住的项只显示。「恢复成默认值」的按钮（上面的 marks）留着：恢复默认是存得上的 */}
            <Control
              field={field}
              value={value}
              disabled={disabled || locked}
              onChange={(next) => onChange(field.path, next)}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/** 往一组键值里加一项（比如给「各阶段的进度文字」加一个阶段）。 */
function AddEntry({
  path,
  taken,
  onChange,
}: {
  path: string[];
  taken: string[];
  onChange: (path: string[], value: unknown) => void;
}) {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [text, setText] = useState("");
  const name = key.trim();
  const clash = taken.includes(name);
  const add = () => {
    if (!name || clash) return;
    let value: unknown = text;
    try {
      value = JSON.parse(text); // 写的是数字、true、列表、{...} 就按那个类型；否则当文字
    } catch {
      value = text;
    }
    onChange([...path, name], value);
    setKey("");
    setText("");
    setOpen(false);
  };
  if (!open) {
    return (
      <Button variant="ghost" size="xs" className="text-muted-foreground" onClick={() => setOpen(true)}>
        <Plus /> 加一项
      </Button>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 py-1">
      <Input
        className="h-8 w-40 font-mono"
        placeholder="名字"
        autoFocus
        value={key}
        aria-invalid={clash}
        onChange={(event) => setKey(event.target.value)}
      />
      <Input
        className="h-8 min-w-48 flex-1"
        placeholder='值：文字直接写；数字、true、["a"]、{"k": 1} 按 JSON 认'
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => event.key === "Enter" && !event.nativeEvent.isComposing && add()}
      />
      <Button size="sm" onClick={add} disabled={!name || clash}>
        加上
      </Button>
      <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
        算了
      </Button>
      {clash ? <span className="text-destructive text-xs">已经有叫这个名字的了</span> : null}
    </div>
  );
}

/** 表单里的一项。是一组的话，里面的项缩进一层接着排。 */
export function FieldRow(props: FieldProps & { depth?: number }) {
  const { field, depth = 0 } = props;
  const [open, setOpen] = useState(true);
  if (!visible(field, props)) return null;
  if (field.kind !== "group") return <Leaf {...props} />;
  const value = getAt(props.draft, field.path);
  const original = getAt(props.base, field.path);
  const changed = !sameValue(original, value);
  const children = field.children ?? [];
  const locked = field.locked === true;
  return (
    <div className={cn(depth > 0 && "py-2")}>
      <div
        className="flex cursor-pointer flex-wrap items-center gap-1.5 py-1.5"
        onClick={() => setOpen(!open)}
      >
        <ChevronDown
          className={cn("text-muted-foreground size-4 transition-transform", !open && "-rotate-90")}
        />
        <code className="font-mono text-[13px] font-semibold">{field.key}</code>
        <span className="text-muted-foreground text-xs">{children.length} 项</span>
        {locked ? <Lock className="text-muted-foreground size-3" /> : null}
        {changed ? <Badge tone="brand">{field.added ? "加的" : "里面有改动"}</Badge> : null}
        {changed && !props.disabled ? (
          <Hint text={field.added ? "去掉这一整组" : "这一组全部恢复成默认值"}>
            <Button
              variant="ghost"
              size="icon-xs"
              className="text-muted-foreground"
              onClick={(event) => {
                event.stopPropagation();
                props.onChange(field.path, original);
              }}
            >
              {field.added ? <Trash2 /> : <RotateCcw />}
            </Button>
          </Hint>
        ) : null}
      </div>
      <Description text={field.description} />
      {locked && !props.inLocked ? <LockedNote changed={changed} added={field.added} /> : null}
      {open ? (
        <div className="mt-1 ml-2 border-l pl-4">
          <div className="divide-y">
            {children.map((child) => (
              <FieldRow
                key={child.key}
                {...props}
                field={child}
                depth={depth + 1}
                inLocked={props.inLocked || locked}
              />
            ))}
          </div>
          {props.disabled || locked || props.filter || props.changedOnly ? null : (
            <AddEntry
              path={field.path}
              taken={children.map((child) => child.key)}
              onChange={props.onChange}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}
