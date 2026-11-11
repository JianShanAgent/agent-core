// 机器人配置：左边是各份配置文件，右边是选中那一份的表单。
// 表单是照默认值文件的结构生成的，每一项的说明就是文件里的注释。改动存成覆写
// （只记和默认值不一样的项），保存后马上重新装配生效；写错了装不起来会被撤回。
// 有几项不能在网页上改（接口地址、用哪个密钥……）：是哪几项由后端告诉页面，页面把它们显示成只读。

import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, FileCog, Power, RotateCcw, Save, Search, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Banner } from "@/components/layout/StateBanners";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Input, Switch, Textarea } from "@/components/ui/form";
import { Hint, Pre } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { useRefreshConfig } from "@/lib/hooks";
import type {
  ConfigFile,
  ConfigFileSummary,
  ConfigValues,
  LockedRules,
  SchemaField,
} from "@/lib/types";
import { cn, getAt, sameValue, setAt } from "@/lib/utils";

import { FieldRow, LOCKED_NOTE } from "./config/fields";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/** 一个列表里面（不管套了几层）有没有锁住的项。 */
function holdsLocked(rules: LockedRules, value: unknown): boolean {
  if (Array.isArray(value)) return value.some((item) => holdsLocked(rules, item));
  if (!isRecord(value)) return false;
  return Object.entries(value).some(
    ([key, item]) => rules.keys.includes(key) || holdsLocked(rules, item),
  );
}

/**
 * 这次编辑里新加出来的项是不是锁住的。后端给的表单结构里没有它，照后端给的名单（rules）判断，
 * 判断的办法和后端一样：它自己或者上面某一层在名单里；或者它是个列表、里面套着名单里的项。
 */
function lockedBy(rules: LockedRules, path: string[], value: unknown): boolean {
  return (
    path.some((key) => rules.keys.includes(key)) ||
    rules.paths.some((locked) => locked.every((key, at) => path[at] === key)) ||
    (Array.isArray(value) && holdsLocked(rules, value))
  );
}

/** 表单里加出来的项，后端给的结构里还没有：按它现在的值补一个。 */
function fieldFor(key: string, value: unknown, path: string[], rules: LockedRules): SchemaField {
  const base = { key, path, description: "", added: true, locked: lockedBy(rules, path, value) };
  if (isRecord(value)) {
    return {
      ...base,
      kind: "group",
      children: Object.entries(value).map(([name, item]) =>
        fieldFor(name, item, [...path, name], rules),
      ),
    };
  }
  if (typeof value === "boolean") return { ...base, kind: "bool" };
  if (typeof value === "number") return { ...base, kind: Number.isInteger(value) ? "int" : "float" };
  if (typeof value === "string") {
    return { ...base, kind: value.includes("\n") || value.length > 80 ? "text" : "str" };
  }
  if (Array.isArray(value) && value.every((item) => typeof item === "string" || typeof item === "number")) {
    return { ...base, kind: "list", items: value.every((item) => typeof item === "number") && value.length ? "number" : "str" };
  }
  return { ...base, kind: "json" };
}

/** 后端给的表单结构 + 这次编辑里加出来、删掉的项。 */
function mergeSchema(
  fields: SchemaField[],
  draft: unknown,
  path: string[],
  rules: LockedRules,
): SchemaField[] {
  const node = isRecord(draft) ? draft : {};
  const merged = fields
    .filter((field) => !(field.added && !(field.key in node)))
    .map((field) =>
      field.kind === "group"
        ? {
            ...field,
            children: mergeSchema(field.children ?? [], node[field.key], field.path, rules),
          }
        : field,
    );
  const known = new Set(fields.map((field) => field.key));
  for (const [key, value] of Object.entries(node)) {
    if (!known.has(key)) merged.push(fieldFor(key, value, [...path, key], rules));
  }
  return merged;
}

/**
 * 锁住的项里，现在和默认值不一样的那些（写成 a.b 的样子）。
 * 带着它们保存一定会被后端拒绝，所以先在页面上说清楚，不让人填完一整页才知道存不上。
 * 表单里锁住的项动不了，会走到这里的是：在「整份 JSON」里改的、新加的项刚好叫锁住的名字、
 * 或者有人手改过服务器上的覆写文件。
 */
function lockedChanges(fields: SchemaField[], base: ConfigValues, draft: ConfigValues): string[] {
  const found: string[] = [];
  for (const field of fields) {
    if (field.locked) {
      if (!sameValue(getAt(base, field.path), getAt(draft, field.path))) {
        found.push(field.path.join("."));
      }
    } else if (field.kind === "group") {
      found.push(...lockedChanges(field.children ?? [], base, draft));
    }
  }
  return found;
}

/** 这一项（或者它下面的某一项）是锁住的。 */
function hasLocked(field: SchemaField): boolean {
  return field.locked === true || (field.children ?? []).some(hasLocked);
}

/** 改了几项（数到最里面的值）。 */
function countChanges(base: unknown, draft: unknown): number {
  if (isRecord(base) && isRecord(draft)) {
    const keys = new Set([...Object.keys(base), ...Object.keys(draft)]);
    let total = 0;
    for (const key of keys) total += countChanges(base[key], draft[key]);
    return total;
  }
  return sameValue(base, draft) ? 0 : 1;
}

function FileList({
  files,
  current,
  onPick,
}: {
  files: ConfigFileSummary[];
  current: string;
  onPick: (name: string) => void;
}) {
  const groups = useMemo(() => {
    const found = new Map<string, ConfigFileSummary[]>();
    for (const file of files) found.set(file.group, [...(found.get(file.group) ?? []), file]);
    return [...found.entries()];
  }, [files]);
  return (
    <div className="flex flex-col gap-3 p-2">
      {groups.map(([group, items]) => (
        <div key={group}>
          <div className="text-muted-foreground px-2 pb-1 text-[11px] font-medium tracking-wider">
            {group}
          </div>
          <div className="flex flex-col gap-0.5">
            {items.map((file) => (
              <button
                key={file.name}
                type="button"
                onClick={() => onPick(file.name)}
                className={cn(
                  "cursor-pointer rounded-md px-2 py-1.5 text-left transition-colors",
                  file.name === current ? "bg-accent" : "hover:bg-accent/60",
                )}
              >
                <div className="flex items-center gap-1.5">
                  <code className="min-w-0 flex-1 truncate font-mono text-[13px] font-medium">
                    {file.name}
                  </code>
                  {file.error ? <Badge tone="danger">出错</Badge> : null}
                  {file.overridden ? <Badge tone="brand">改了 {file.overridden} 项</Badge> : null}
                </div>
                <div className="text-muted-foreground mt-0.5 line-clamp-1 text-xs">
                  {file.summary || "（没有说明）"}
                </div>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function Editor({ file, onDirty }: { file: ConfigFile; onDirty: (dirty: boolean) => void }) {
  const refresh = useRefreshConfig();
  const [draft, setDraft] = useState<ConfigValues>(file.effective);
  const [filter, setFilter] = useState("");
  const [changedOnly, setChangedOnly] = useState(false);
  const [json, setJson] = useState<string | null>(null); // 「整份 JSON」那一页里正在改的文字
  const [jsonError, setJsonError] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);
  const [showAll, setShowAll] = useState(false); // 文件开头的说明很长时先只显示三行
  const longDescription = file.description.split("\n").length > 3;

  // 换了文件、或者保存后拿到了新的内容：编辑的内容跟着换
  useEffect(() => {
    setDraft(file.effective);
    setJson(null);
    setJsonError("");
  }, [file.name, file.effective]);

  const dirty = !sameValue(draft, file.effective);
  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  const writable = file.state.writable;
  const schema = useMemo(
    () => mergeSchema(file.schema, draft, [], file.locked),
    [file.schema, file.locked, draft],
  );
  const pending = countChanges(file.effective, draft); // 这次还没保存的
  const fromDefault = countChanges(file.base, draft); // 相对默认值一共改了的
  const blocked = useMemo(() => lockedChanges(schema, file.base, draft), [schema, file.base, draft]);

  const save = useMutation({
    mutationFn: (values: ConfigValues) => api.saveConfig(file.name, values),
    onSuccess: (result) => {
      if (result.needs_restart.length) {
        toast.success("存好了，重启机器人后生效", {
          description: "这份配置管的是启动时就定下来的东西。页面上方有「重启机器人」。",
        });
      } else {
        toast.success("存好了，已经生效");
      }
      refresh();
    },
    onError: (error: Error) => {
      toast.error("没存成，机器人仍按原来的配置运行", { description: error.message, duration: 12_000 });
      refresh();
    },
  });

  const change = (path: string[], value: unknown) => {
    setDraft((current) => setAt(current, path, value));
    setJson(null);
  };

  return (
    <div className="flex min-w-0 flex-col">
      <div className="border-b p-4">
        <div className="flex flex-wrap items-center gap-2">
          <code className="font-mono text-base font-semibold">{file.name}</code>
          {file.mode === "restart" ? (
            <Hint text="这份配置管的是数据库、QQ 连接、HTTP 服务这些启动时就定下来的东西">
              <Badge tone="warning">
                <Power /> 改了要重启才生效
              </Badge>
            </Hint>
          ) : (
            <Badge tone="success">
              <Zap /> 保存后马上生效
            </Badge>
          )}
          {fromDefault ? <Badge tone="brand">和默认值有 {fromDefault} 处不一样</Badge> : null}
        </div>
        {file.description ? (
          <p
            className={cn(
              "text-muted-foreground mt-2 max-w-4xl text-xs leading-relaxed whitespace-pre-line",
              longDescription && "cursor-pointer",
              longDescription && !showAll && "line-clamp-3",
            )}
            title={longDescription && !showAll ? "点一下看完整的说明" : undefined}
            onClick={() => longDescription && setShowAll(!showAll)}
          >
            {file.description}
          </p>
        ) : null}
        {file.error ? (
          <Banner tone="danger" className="mt-3 mb-0">
            这份配置的覆写文件没用上：{file.error}。现在用的是默认值；在这里保存一次会把它重写成正确的。
          </Banner>
        ) : null}
        {!writable ? (
          <Banner tone="info" className="mt-3 mb-0">
            没有配置覆写目录（环境变量 <code className="font-mono text-xs">JIANSHANAGENT_CONFIG_OVERRIDES_DIR</code>
            ），改动没有地方存，这里只能看。
          </Banner>
        ) : null}
        {blocked.length ? (
          <Banner tone="warning" className="mt-3 mb-0">
            <span className="font-mono text-xs">{blocked.join("、")}</span>{" "}
            不能在网页上改，现在却和默认值不一样，带着它这份配置存不上。把它恢复成默认值（新加的就去掉）再保存；真要改它，改仓库里的配置文件再发布。
          </Banner>
        ) : null}
      </div>

      <Tabs defaultValue="form" className="gap-0">
        <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
          <TabsList>
            <TabsTrigger value="form">表单</TabsTrigger>
            <TabsTrigger value="json">整份 JSON</TabsTrigger>
            <TabsTrigger value="source">默认值原文</TabsTrigger>
          </TabsList>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {fromDefault && writable ? (
              <Button variant="ghost" size="sm" onClick={() => setConfirmReset(true)}>
                <RotateCcw /> 全部恢复默认
              </Button>
            ) : null}
            {dirty ? (
              <Button variant="outline" size="sm" onClick={() => setDraft(file.effective)}>
                放弃改动
              </Button>
            ) : null}
            <Button
              size="sm"
              variant={dirty ? "brand" : "default"}
              disabled={!dirty || !writable || Boolean(jsonError) || blocked.length > 0}
              loading={save.isPending}
              onClick={() => save.mutate(draft)}
            >
              {save.isPending ? null : <Save />}
              {dirty ? `保存 ${pending} 处改动` : "没有改动"}
            </Button>
          </div>
        </div>

        <TabsContent value="form" className="px-4 pb-4">
          <div className="flex flex-wrap items-center gap-3 py-3">
            <div className="relative max-w-xs flex-1">
              <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
              <Input
                className="h-8 pl-8"
                placeholder="找一项：名字或说明里的字"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
              />
            </div>
            <label className="flex cursor-pointer items-center gap-2 text-sm">
              <Switch checked={changedOnly} onCheckedChange={setChangedOnly} />
              只看改过的
            </label>
          </div>
          <div className="divide-y border-t">
            {schema.map((field) => (
              <FieldRow
                key={field.key}
                field={field}
                base={file.base}
                draft={draft}
                disabled={!writable || save.isPending}
                filter={filter.trim().toLowerCase()}
                changedOnly={changedOnly}
                onChange={change}
              />
            ))}
          </div>
          {changedOnly && fromDefault === 0 ? (
            <EmptyState title="这份配置和默认值完全一样" className="py-8" />
          ) : null}
        </TabsContent>

        <TabsContent value="json" className="p-4">
          <p className="text-muted-foreground mb-2 text-xs">
            这份配置现在生效的完整内容。要加一整段、删掉某一项、调整结构时在这里改；保存时只会把和默认值不一样的地方存下来。
            {file.schema.some(hasLocked) ? `表单里带锁的那几项在这里也一样：${LOCKED_NOTE}。` : null}
          </p>
          <Textarea
            className="min-h-[28rem] font-mono text-xs"
            spellCheck={false}
            disabled={!writable || save.isPending}
            value={json ?? JSON.stringify(draft, null, 2)}
            aria-invalid={Boolean(jsonError)}
            onChange={(event) => {
              const text = event.target.value;
              setJson(text);
              try {
                const parsed: unknown = JSON.parse(text);
                if (!isRecord(parsed)) throw new Error("最外层要是 { … }");
                setDraft(parsed);
                setJsonError("");
              } catch (error) {
                setJsonError((error as Error).message);
              }
            }}
          />
          {jsonError ? (
            <div className="text-destructive mt-1.5 text-xs">不是合法的 JSON：{jsonError}</div>
          ) : null}
        </TabsContent>

        <TabsContent value="source" className="flex flex-col gap-4 p-4">
          <div>
            <div className="mb-1.5 text-sm font-medium">
              默认值：<code className="font-mono text-xs">config/{file.name}.yaml</code>
            </div>
            <p className="text-muted-foreground mb-2 text-xs">
              随发布走的那一份，带着完整的注释。网页上改不了它，要改它就改仓库里的文件再发布。
            </p>
            <Pre className="max-h-[32rem] overflow-y-auto">{file.source || "（空的）"}</Pre>
          </div>
          <div>
            <div className="mb-1.5 text-sm font-medium">覆写文件现在的内容</div>
            <p className="text-muted-foreground mb-2 text-xs">
              网页上的改动存在这里，只有和默认值不一样的项。发布换版本它不会被盖掉。
            </p>
            <Pre>{file.override_text || "（没有覆写：全部是默认值）"}</Pre>
          </div>
        </TabsContent>
      </Tabs>

      <ConfirmDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title={`把 ${file.name} 全部恢复成默认值？`}
        description={`会去掉这份配置在网页上做过的全部 ${fromDefault} 处改动，${
          file.mode === "restart" ? "重启机器人后生效" : "马上生效"
        }。`}
        confirmText="恢复默认"
        destructive
        loading={save.isPending}
        onConfirm={() => {
          setConfirmReset(false);
          save.mutate(file.base);
        }}
      />
    </div>
  );
}

export default function Config() {
  const name = useParams()["*"] ?? "";
  const navigate = useNavigate();
  const [dirty, setDirty] = useState(false);
  const [leaving, setLeaving] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["config", "list"], queryFn: api.configList });
  const file = useQuery({
    queryKey: ["config", "file", name],
    queryFn: () => api.config(name),
    enabled: name !== "",
  });

  const pick = (next: string) => {
    if (next === name) return;
    if (dirty) setLeaving(next);
    else navigate(next ? `/config/${next}` : "/config");
  };

  return (
    <div>
      <PageHeader
        title="机器人配置"
        description="每一项的说明就是配置文件里的注释。在这里改的存成覆写，发布新版本不会被盖掉；写错了装不起来会自动撤回，机器人照原样运行。凭据不在这里。"
      />
      <div className="grid items-start gap-3 lg:grid-cols-[17rem_minmax(0,1fr)]">
        <Card className={cn("lg:sticky lg:top-0", name && "hidden lg:block")}>
          {list.isPending ? (
            <LoadingRows />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : (
            <FileList files={list.data.files} current={name} onPick={pick} />
          )}
        </Card>
        <Card className={cn("min-w-0", !name && "hidden lg:block")}>
          {name ? (
            <Button
              variant="ghost"
              size="sm"
              className="text-muted-foreground m-2 mb-0 lg:hidden"
              onClick={() => pick("")}
            >
              <ArrowLeft /> 全部配置
            </Button>
          ) : null}
          {!name ? (
            <EmptyState
              icon={FileCog}
              title="选一份配置"
              hint="左边按用途分了组：全局、接入（QQ 网关）、内核（路由、模型、存储、插件清单）、各个插件、人设。"
            />
          ) : file.isPending ? (
            <LoadingRows rows={8} />
          ) : file.isError ? (
            <ErrorState error={file.error} onRetry={() => void file.refetch()} />
          ) : (
            <Editor key={file.data.name} file={file.data} onDirty={setDirty} />
          )}
        </Card>
      </div>
      <ConfirmDialog
        open={leaving !== null}
        onOpenChange={(open) => !open && setLeaving(null)}
        title="有没保存的改动"
        description="换到别的配置的话，这份上没保存的改动就丢了。"
        confirmText="不要了，换过去"
        destructive
        onConfirm={() => {
          const next = leaving ?? "";
          setLeaving(null);
          setDirty(false);
          navigate(next ? `/config/${next}` : "/config");
        }}
      />
    </div>
  );
}
