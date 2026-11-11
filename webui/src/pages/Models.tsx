// 模型管理：有哪几档模型（型号、开不开思考、长度上限），处理流程的各个环节各用哪一档。
// 两样分别写在 core.llm 和 plugin.research 两份配置里，这里一起改、一起保存（要么都成要么都撤回）。

import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowRight, Brain, FlaskConical, Lock, Plus, Save, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

import { ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Banner } from "@/components/layout/StateBanners";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Label, Switch } from "@/components/ui/form";
import { Hint, Table, Td, Th, Tr } from "@/components/ui/misc";
import { SimpleSelect } from "@/components/ui/select";
import { api } from "@/lib/api";
import { formatAgo, formatNumber, formatSeconds, formatTokens } from "@/lib/format";
import { useRefreshConfig } from "@/lib/hooks";
import type { ConfigValues, ModelsInfo, ModelTestResult } from "@/lib/types";
import { cn, getAt, sameValue, setAt } from "@/lib/utils";

interface ProfileValues {
  model?: string;
  thinking?: boolean;
  max_completion_tokens?: number;
  temperature?: number;
}

const NAME_RULE = /^[a-z][a-z0-9_]*$/;

function profilesOf(llm: ConfigValues): Record<string, ProfileValues> {
  const profiles = llm.profiles;
  return profiles !== null && typeof profiles === "object" && !Array.isArray(profiles)
    ? (profiles as Record<string, ProfileValues>)
    : {};
}

/** 数字框：空着表示「不设」（比如温度）。 */
function OptionalNumber({
  value,
  onChange,
  disabled,
  placeholder,
  integer = false,
}: {
  value: number | undefined;
  onChange: (value: number | undefined) => void;
  disabled?: boolean;
  placeholder?: string;
  integer?: boolean;
}) {
  const [text, setText] = useState(value === undefined ? "" : String(value));
  useEffect(() => {
    if ((text === "" ? undefined : Number(text)) !== value) {
      setText(value === undefined ? "" : String(value));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const number = Number(text);
  const valid = text.trim() === "" || (Number.isFinite(number) && (!integer || Number.isInteger(number)));
  return (
    <Input
      inputMode="decimal"
      className="h-8 tabular-nums"
      value={text}
      disabled={disabled}
      placeholder={placeholder}
      aria-invalid={!valid}
      onChange={(event) => {
        const next = event.target.value;
        setText(next);
        if (next.trim() === "") onChange(undefined);
        else if (Number.isFinite(Number(next)) && (!integer || Number.isInteger(Number(next)))) {
          onChange(Number(next));
        }
      }}
    />
  );
}

function TestResult({ result }: { result: ModelTestResult }) {
  if (!result.ok) {
    return (
      <div className="text-destructive bg-destructive/8 rounded-md px-2.5 py-1.5 text-xs">
        没通（{formatSeconds(result.seconds)}）：{result.error}
      </div>
    );
  }
  const odd = result.finish_reason && !["stop", "tool_calls"].includes(result.finish_reason);
  return (
    <div className="border-success/30 bg-success/8 rounded-md border px-2.5 py-1.5 text-xs leading-relaxed">
      <span className="text-success font-medium">通了，{formatSeconds(result.seconds)}</span>
      <span className="text-muted-foreground">
        {" "}
        · {result.model} · 输入 {result.prompt_tokens} · 输出 {result.completion_tokens}
        {result.reasoning_tokens ? `（思考 ${result.reasoning_tokens}）` : ""}
        {odd ? ` · 结束原因 ${result.finish_reason}` : ""}
      </span>
      <div className="mt-0.5">它回的是：{result.content || "（空的）"}</div>
    </div>
  );
}

function ProfileCard({
  name,
  values,
  info,
  usedBy,
  isDefault,
  dirty,
  disabled,
  onChange,
  onRemove,
}: {
  name: string;
  values: ProfileValues;
  info: ModelsInfo["profiles"][number] | undefined;
  usedBy: string[];
  isDefault: boolean;
  dirty: boolean;
  disabled: boolean;
  onChange: (key: keyof ProfileValues, value: unknown) => void;
  onRemove: () => void;
}) {
  const test = useMutation({ mutationFn: () => api.testModel(name) });
  const usage = info?.usage;
  const removable = usedBy.length === 0 && !isDefault;
  return (
    <Card className={cn(dirty && "border-brand/50")}>
      <CardHeader className="flex-nowrap pb-1">
        <div className="min-w-0 flex-1">
          <CardTitle className="flex flex-wrap items-center gap-1.5">
            <code className="font-mono text-sm">{name}</code>
            {isDefault ? <Badge tone="muted">默认档</Badge> : null}
            {!info ? <Badge tone="brand">新加的</Badge> : dirty ? <Badge tone="brand">改了，没保存</Badge> : null}
          </CardTitle>
          {info?.description ? (
            <CardDescription className="line-clamp-3 whitespace-pre-line" title={info.description}>
              {info.description}
            </CardDescription>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Hint text={dirty || !info ? "先保存，再测试（测的是正在运行的配置）" : "给它发一句话，看通不通、多快"}>
            <span>
              <Button
                variant="outline"
                size="xs"
                disabled={dirty || !info || disabled}
                loading={test.isPending}
                onClick={() => test.mutate()}
              >
                {test.isPending ? null : <FlaskConical />} 测试
              </Button>
            </span>
          </Hint>
          <Hint
            text={
              removable
                ? "删掉这一档"
                : isDefault
                  ? "它是默认档，删不了"
                  : `「${usedBy.join("、")}」在用它，先给它们换一档`
            }
          >
            <span>
              <Button
                variant="ghost"
                size="icon-xs"
                className="text-muted-foreground"
                disabled={!removable || disabled}
                onClick={onRemove}
              >
                <Trash2 />
              </Button>
            </span>
          </Hint>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-x-3 gap-y-2.5">
          <div className="col-span-2">
            <Label className="text-muted-foreground text-xs font-normal">型号</Label>
            <Input
              className="mt-1 h-8 font-mono"
              value={values.model ?? ""}
              disabled={disabled}
              spellCheck={false}
              onChange={(event) => onChange("model", event.target.value)}
            />
          </div>
          <div>
            <Label className="text-muted-foreground text-xs font-normal">
              最多生成多少 token{values.thinking ? "（含思考）" : ""}
            </Label>
            <div className="mt-1">
              <OptionalNumber
                integer
                value={values.max_completion_tokens}
                disabled={disabled}
                placeholder="2048"
                onChange={(value) => onChange("max_completion_tokens", value)}
              />
            </div>
          </div>
          <div>
            <Label className="text-muted-foreground text-xs font-normal">
              温度{values.thinking ? "（开思考时不生效）" : ""}
            </Label>
            <div className="mt-1">
              <OptionalNumber
                value={values.temperature}
                disabled={disabled || values.thinking === true}
                placeholder="不设"
                onChange={(value) => onChange("temperature", value)}
              />
            </div>
          </div>
        </div>
        <label className="flex cursor-pointer items-center justify-between gap-3 rounded-md border px-3 py-2">
          <span className="flex items-center gap-2 text-sm">
            <Brain className={cn("size-4", values.thinking ? "text-info" : "text-muted-foreground")} />
            开思考
            <span className="text-muted-foreground text-xs">
              {values.thinking ? "答得准，但一次要多等几秒到一分钟" : "快，但不适合要推理、要查证的环节"}
            </span>
          </span>
          <Switch
            checked={values.thinking === true}
            disabled={disabled}
            onCheckedChange={(on) => onChange("thinking", on)}
          />
        </label>
        <div className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span>
            {usedBy.length ? (
              <>
                在用它的：
                {usedBy.map((role) => (
                  <Badge key={role} tone="outline" className="ml-1">
                    {role}
                  </Badge>
                ))}
              </>
            ) : (
              "现在没有环节在用它"
            )}
          </span>
        </div>
        <div className="text-muted-foreground border-t pt-2 text-xs">
          {usage ? (
            <>
              最近 24 小时：调用 {formatNumber(usage.calls)} 次
              {usage.failed ? <span className="text-destructive">（{usage.failed} 次没成）</span> : ""}
              ，平均 {formatSeconds(usage.avg_seconds)}，九成在 {formatSeconds(usage.p90_seconds)} 内，共{" "}
              {formatTokens(usage.tokens)} token；最近一次{formatAgo(usage.last_at)}
            </>
          ) : (
            "最近 24 小时没有调用过"
          )}
        </div>
        {test.data ? <TestResult result={test.data} /> : null}
        {test.isError ? (
          <div className="text-destructive text-xs">测试没发出去：{test.error.message}</div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function AddProfile({
  open,
  taken,
  onClose,
  onAdd,
}: {
  open: boolean;
  taken: string[];
  onClose: () => void;
  onAdd: (name: string, values: ProfileValues) => void;
}) {
  const [name, setName] = useState("");
  const [model, setModel] = useState("");
  const [thinking, setThinking] = useState(false);
  useEffect(() => {
    if (open) {
      setName("");
      setModel("");
      setThinking(false);
    }
  }, [open]);
  const problem = !name
    ? ""
    : !NAME_RULE.test(name)
      ? "名字用小写字母开头，只含小写字母、数字、下划线"
      : taken.includes(name)
        ? "已经有叫这个名字的档位了"
        : "";
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>加一档模型</DialogTitle>
          <DialogDescription>
            加完以后在下面「各环节用哪一档」里把它派给某个环节，再保存。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div>
            <Label>名字</Label>
            <Input
              className="mt-1.5 font-mono"
              placeholder="例如 fast"
              autoFocus
              value={name}
              aria-invalid={Boolean(problem)}
              onChange={(event) => setName(event.target.value.trim())}
            />
            {problem ? <div className="text-destructive mt-1 text-xs">{problem}</div> : null}
          </div>
          <div>
            <Label>型号</Label>
            <Input
              className="mt-1.5 font-mono"
              placeholder="例如 mimo-v2.6-flash"
              value={model}
              onChange={(event) => setModel(event.target.value.trim())}
            />
          </div>
          <label className="flex cursor-pointer items-center justify-between rounded-md border px-3 py-2 text-sm">
            开思考
            <Switch checked={thinking} onCheckedChange={setThinking} />
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={!name || !model || Boolean(problem)}
            onClick={() => {
              onAdd(name, {
                model,
                thinking,
                max_completion_tokens: thinking ? 4096 : 2048,
                ...(thinking ? {} : { temperature: 0.2 }),
              });
              onClose();
            }}
          >
            加上
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Editor({ info }: { info: ModelsInfo }) {
  const refresh = useRefreshConfig();
  const [llm, setLlm] = useState<ConfigValues>(info.llm);
  const [research, setResearch] = useState<ConfigValues>(info.research);
  const [adding, setAdding] = useState(false);
  const dirty = !sameValue(llm, info.llm) || !sameValue(research, info.research);

  // 后台刷新拿到了新内容：没在改的时候才跟着换，不然会把没保存的改动冲掉
  const synced = useRef({ llm: info.llm, research: info.research });
  useEffect(() => {
    const changed =
      !sameValue(synced.current.llm, info.llm) || !sameValue(synced.current.research, info.research);
    if (!changed) return;
    const wasDirty = !sameValue(llm, synced.current.llm) || !sameValue(research, synced.current.research);
    synced.current = { llm: info.llm, research: info.research };
    if (!wasDirty) {
      setLlm(info.llm);
      setResearch(info.research);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info.llm, info.research]);

  const profiles = profilesOf(llm);
  const names = Object.keys(profiles);
  const saved = profilesOf(info.llm);
  const writable = info.state.writable;
  const defaultProfile = String(llm.default_profile ?? "");
  const roleProfile = (role: ModelsInfo["roles"][number]) =>
    role.file ? String(getAt(research, role.path) ?? "") : role.profile;
  const usedBy = (name: string) =>
    info.roles.filter((role) => roleProfile(role) === name).map((role) => role.label);
  const missing = info.roles.filter((role) => roleProfile(role) && !names.includes(roleProfile(role)));

  const save = useMutation({
    mutationFn: () => {
      const files: Record<string, ConfigValues> = {};
      if (!sameValue(llm, info.llm)) files["core.llm"] = llm;
      if (!sameValue(research, info.research)) files["plugin.research"] = research;
      return api.saveConfigs(files);
    },
    onSuccess: () => {
      toast.success("存好了，已经生效", { description: "之后的消息就按新的来。" });
      synced.current = { llm, research };
      refresh();
    },
    onError: (error: Error) => {
      toast.error("没存成，机器人仍按原来的配置运行", { description: error.message, duration: 12_000 });
      refresh();
    },
  });
  const busy = save.isPending;

  return (
    <div className="flex flex-col gap-4">
      {!info.enabled ? (
        <Banner tone="warning">
          大模型现在是关着的（core.llm 的 enabled 是 false）：机器人只回命令，不回别的话。
        </Banner>
      ) : !info.running ? (
        <Banner tone="warning">配置里开了大模型，但正在运行的这一套里没有它（可能还没重新加载）。</Banner>
      ) : null}
      {!writable ? (
        <Banner tone="info">
          没有配置覆写目录（环境变量 <code className="font-mono text-xs">JIANSHANAGENT_CONFIG_OVERRIDES_DIR</code>
          ），这里只能看不能改。
        </Banner>
      ) : null}

      <Card>
        <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-2 p-4 text-sm">
          <div>
            <div className="text-muted-foreground text-xs">接口地址</div>
            <code className="font-mono text-[13px]">{info.base_url}</code>
            {info.base_url_locked ? (
              // 密钥是发往这个地址的，所以它不能在网页上改（是不是锁着由后端说了算）
              <div className="text-muted-foreground mt-0.5 flex items-center gap-1 text-xs">
                <Lock className="size-3 shrink-0" />
                不能在网页上改，要改仓库里的配置文件再发布
              </div>
            ) : null}
          </div>
          <div>
            <div className="text-muted-foreground text-xs">一次调用最多等</div>
            {info.timeout_seconds} 秒
          </div>
          <div>
            <div className="text-muted-foreground text-xs">联网搜索</div>
            {info.web_search ? <Badge tone="success">开着</Badge> : <Badge tone="muted">没开</Badge>}
          </div>
          <div>
            <div className="text-muted-foreground text-xs">密钥</div>
            在 secrets.yaml 里，网页上不显示
          </div>
          <Button asChild variant="ghost" size="sm" className="ml-auto">
            <Link to="/config/core.llm">
              这些在配置里改 <ArrowRight />
            </Link>
          </Button>
        </CardContent>
      </Card>

      <div>
        <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">档位</h2>
            <p className="text-muted-foreground text-xs">
              一档 = 一个型号加一组参数。型号只写在这里，换型号不用动别处。
            </p>
          </div>
          <Button variant="outline" size="sm" disabled={!writable || busy} onClick={() => setAdding(true)}>
            <Plus /> 加一档
          </Button>
        </div>
        <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {names.map((name) => (
            <ProfileCard
              key={name}
              name={name}
              values={profiles[name] ?? {}}
              info={info.profiles.find((item) => item.name === name && name in saved)}
              usedBy={usedBy(name)}
              isDefault={name === defaultProfile}
              dirty={!sameValue(profiles[name], saved[name])}
              disabled={!writable || busy}
              onChange={(key, value) => setLlm(setAt(llm, ["profiles", name, key], value))}
              onRemove={() => setLlm(setAt(llm, ["profiles", name], undefined))}
            />
          ))}
        </div>
      </div>

      <Card>
        <CardHeader>
          <div>
            <CardTitle>各环节用哪一档</CardTitle>
            <CardDescription>
              一条消息的处理过程里，每个环节各调一次模型。查证员和复核员最慢也最要紧：开思考答得准，不开快得多。
            </CardDescription>
          </div>
        </CardHeader>
        <Table>
          <thead>
            <tr>
              <Th className="w-40">环节</Th>
              <Th>它做什么</Th>
              <Th className="w-56">用哪一档</Th>
            </tr>
          </thead>
          <tbody>
            {info.roles.map((role) => {
              const current = roleProfile(role);
              const chosen = profiles[current];
              const changed = role.file ? current !== String(getAt(info.research, role.path) ?? "") : false;
              return (
                <Tr key={role.id}>
                  <Td className="font-medium whitespace-nowrap">
                    {role.label}
                    {changed ? (
                      <Badge tone="brand" className="ml-1.5">
                        改了
                      </Badge>
                    ) : null}
                  </Td>
                  <Td className="text-muted-foreground text-xs">{role.hint}</Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      {role.file ? (
                        <SimpleSelect
                          size="sm"
                          className="w-32"
                          value={current}
                          disabled={!writable || busy}
                          onChange={(value) => setResearch(setAt(research, role.path, value))}
                          options={[
                            ...(names.includes(current) || !current
                              ? []
                              : [{ value: current, label: `${current}（没有这一档）` }]),
                            ...names.map((name) => ({ value: name, label: name })),
                          ]}
                        />
                      ) : (
                        <Hint text="对话入口固定用 chat 这一档，写在代码里；要换型号就改 chat 这一档本身">
                          <code className="bg-muted rounded px-2 py-1 font-mono text-xs">{current}</code>
                        </Hint>
                      )}
                      {chosen ? (
                        <span className="text-muted-foreground truncate text-xs">
                          {chosen.model}
                          {chosen.thinking ? " · 开思考" : ""}
                        </span>
                      ) : null}
                    </div>
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
        <div className="flex flex-wrap items-center gap-3 border-t px-4 py-3">
          <span className="text-muted-foreground text-xs">没指定档位的调用用默认档：</span>
          <SimpleSelect
            size="sm"
            className="w-32"
            value={defaultProfile}
            disabled={!writable || busy}
            onChange={(value) => setLlm(setAt(llm, ["default_profile"], value))}
            options={names.map((name) => ({ value: name, label: name }))}
          />
        </div>
      </Card>

      {missing.length ? (
        <Banner tone="danger">
          {missing.map((role) => `「${role.label}」用的「${roleProfile(role)}」`).join("、")}
          已经没有这一档了。这样保存会被拒绝，先给它们换一档。
        </Banner>
      ) : null}

      {dirty ? (
        <div className="bg-card sticky bottom-3 z-10 flex flex-wrap items-center justify-end gap-2 rounded-xl border p-3 shadow-lg">
          <span className="text-muted-foreground mr-auto text-sm">
            有没保存的改动。保存后马上生效，装不起来会自动撤回。
          </span>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => {
              setLlm(info.llm);
              setResearch(info.research);
            }}
          >
            放弃改动
          </Button>
          <Button variant="brand" loading={busy} disabled={!writable} onClick={() => save.mutate()}>
            {busy ? null : <Save />} 保存
          </Button>
        </div>
      ) : null}

      <AddProfile
        open={adding}
        taken={names}
        onClose={() => setAdding(false)}
        onAdd={(name, values) => setLlm(setAt(llm, ["profiles", name], values))}
      />
    </div>
  );
}

export default function Models() {
  const query = useQuery({ queryKey: ["models"], queryFn: api.models });
  return (
    <div>
      <PageHeader
        title="模型管理"
        description="机器人用哪些模型、每个环节用哪一档。想知道「查证员不开思考会快多少」，在这里换一档、保存，下一条消息就按新的来。"
      />
      {query.isPending ? (
        <LoadingRows rows={8} />
      ) : query.isError ? (
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      ) : (
        <Editor info={query.data} />
      )}
    </div>
  );
}
