// 资料来源：Wiki 管理入口、文档导入，以及微博目标配置和 worker 采集控制。
// 登录会话、目标作者与管理员指定的来源身份相互独立。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  BookOpen,
  CheckCircle,
  ExternalLink,
  Globe,
  Loader2,
  Plus,
  QrCode,
  RefreshCw,
  Upload,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Banner } from "@/components/layout/StateBanners";
import { Badge } from "@/components/ui/badge";
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
import { Input, Label, Switch } from "@/components/ui/form";
import { SimpleSelect } from "@/components/ui/select";
import { Hint, Pre, Skeleton, Spinner } from "@/components/ui/misc";
import { api, ApiError } from "@/lib/api";
import { formatAgo } from "@/lib/format";
import type { CollectorStatus, ImportJob, ImportPreview, ImportState, ImportStatus, SourceInfo, WeiboCollectorConfig, WeiboSessionStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 来源 ID 格式：小写字母开头，只含小写字母/数字/下划线，1-32 位 */
const SOURCE_ID = /^[a-z][a-z0-9_]{0,31}$/;

const ROLE_LABELS: Record<SourceInfo["role"], string> = {
  unknown: "身份未知",
  official: "官方",
  community: "社区",
};

const ROLE_TONES: Record<SourceInfo["role"], "muted" | "brand" | "info"> = {
  unknown: "muted",
  official: "brand",
  community: "info",
};

const IMPORT_STATE_LABELS: Record<ImportState, string> = {
  validating: "校验中",
  embedding: "向量化中",
  committing: "写入中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  recovery_required: "需要恢复",
  interrupted: "已中断",
};

const IMPORT_STATE_TONES: Record<
  ImportState,
  "muted" | "info" | "success" | "danger" | "warning"
> = {
  validating: "info",
  embedding: "info",
  committing: "info",
  completed: "success",
  failed: "danger",
  cancelled: "muted",
  recovery_required: "warning",
  interrupted: "muted",
};

function importIsActive(state: ImportState): boolean {
  return state === "validating" || state === "embedding" || state === "committing";
}

function ImportBadge({ status }: { status: ImportStatus }) {
  const tone = IMPORT_STATE_TONES[status.state];
  const label = IMPORT_STATE_LABELS[status.state];
  const active = importIsActive(status.state);
  return (
    <Badge tone={tone} className="gap-1">
      {active ? <Loader2 className="size-3 animate-spin" /> : null}
      {label}
    </Badge>
  );
}

/** 格式化知识库数量显示 */
function fmtCount(n: number): string {
  return n.toLocaleString("en-US");
}

/** 安全展示 JSON 对象（单条样本） */
function SampleRow({ item }: { item: Record<string, unknown> }) {
  const fields = Object.entries(item).filter(([k]) => k !== "text");
  const text = typeof item["text"] === "string" ? item["text"] : null;
  return (
    <div className="border-b py-2 text-xs last:border-b-0">
      <div className="text-muted-foreground mb-1 flex flex-wrap gap-x-3 gap-y-0.5">
        {fields.map(([k, v]) => (
          <span key={k}>
            <span className="font-medium">{k}：</span>
            {String(v ?? "—")}
          </span>
        ))}
      </div>
      {text ? (
        <div className="text-foreground/80 line-clamp-2 leading-relaxed">{text}</div>
      ) : null}
    </div>
  );
}

/** 预览/导入结果的统计行 */
function PreviewStats({ data }: { data: ImportPreview }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
      <span>
        共 <strong>{fmtCount(data.records)}</strong> 条记录
      </span>
      <span className="text-success">
        新增 <strong>{fmtCount(data.new)}</strong>
      </span>
      {data.updated > 0 ? (
        <span className="text-info">
          更新 <strong>{fmtCount(data.updated)}</strong>
        </span>
      ) : null}
      {data.unchanged > 0 ? (
        <span className="text-muted-foreground">
          未变 <strong>{fmtCount(data.unchanged)}</strong>
        </span>
      ) : null}
      {data.empty ? (
        <span className="text-warning">空批次（不会删除已有资料）</span>
      ) : null}
      <span>
        → 预计 <strong>{fmtCount(data.chunks)}</strong> 个切片
      </span>
    </div>
  );
}

const DEFAULT_COLLECTOR: WeiboCollectorConfig = {
  uid: "", enabled: false, initial_days: 30, overlap_days: 7,
  max_posts: 200, interval_seconds: 1800, include_reposts: true,
};

const COLLECTOR_NUMBER_FIELDS = [
  { key: "initial_days", label: "首次回溯（天）", min: 1, max: 365 },
  { key: "overlap_days", label: "增量回扫（天）", min: 1, max: 30 },
  { key: "max_posts", label: "每轮最多帖子", min: 20, max: 500 },
  { key: "interval_seconds", label: "更新周期（秒）", min: 900, max: 86400 },
] as const;

export function collectorProblem(config: WeiboCollectorConfig, sourceId?: string): string {
  if (!config.uid.trim()) return config.enabled || config.mode === "full_then_incremental" || config.include_comments || config.include_replies ? "请先填写目标主页或 UID" : "";
  if (config.include_replies && !config.include_comments) return "采集楼中楼需要同时包含评论";
  if (config.include_comments) {
    if (config.mode !== "full_then_incremental") return "采集评论及楼中楼需先选择「全部历史 → 核验后增量」模式";
    const discussionId = config.discussion_source_id?.trim() ?? "";
    if (!SOURCE_ID.test(discussionId)) return "请填写有效的社区讨论分区 ID（小写字母开头，最多 32 位）";
    if (discussionId === sourceId) return "社区讨论分区不能使用主来源 ID";
  }
  for (const { key, label, min, max } of COLLECTOR_NUMBER_FIELDS) {
    if (!Number.isInteger(config[key]) || config[key] < min || config[key] > max) {
      return `${label}需为 ${min}–${max} 之间的整数`;
    }
  }
  return "";
}

export function collectorConfirmation(config: WeiboCollectorConfig, previous?: WeiboCollectorConfig | null): string | null {
  const full = config.mode === "full_then_incremental";
  const expanded = (full && previous?.mode !== "full_then_incremental")
    || (!!config.include_comments && !previous?.include_comments)
    || (!!config.include_replies && !previous?.include_replies)
    || (!!config.include_comments && config.discussion_source_id !== previous?.discussion_source_id);
  if (expanded) return `确认保存采集范围：${full ? "全部可访问历史帖子，不受首次回溯天数限制" : "最近窗口帖子"}${config.include_comments ? "、评论" : "（不含评论）"}${config.include_replies ? "、楼中楼回复" : ""}。${config.include_comments ? `讨论单独保存到社区分区 ${config.discussion_source_id}，不会变成官方发言。` : ""}${full ? "历史队列、索引和核验全部完成后才进入定时增量；资源不足会保留断点并阻塞，不保证固定篇数或完成时间。" : ""}${config.enabled ? "保存后将自动运行。" : "保存后可手动启动。"}后续有界批次自动续采，不逐批确认。`;
  return config.enabled && !previous?.enabled
    ? "开启定时后会自动启动采集，并按所选模式继续运行。确认保存并开启？" : null;
}

function confirmSchedule(config: WeiboCollectorConfig, previous?: WeiboCollectorConfig | null): boolean {
  const message = collectorConfirmation(config, previous);
  return !message || window.confirm(message);
}

export function CollectorFields({ value, onChange, disabled, uidLocked = false }: {
  value: WeiboCollectorConfig;
  onChange: (value: WeiboCollectorConfig) => void;
  disabled: boolean;
  uidLocked?: boolean;
}) {
  const problem = collectorProblem(value);
  return (
    <fieldset disabled={disabled} className="flex flex-col gap-3 rounded-lg border p-3">
      <legend className="px-1 text-sm font-medium">微博公开目标账号</legend>
      <label className="flex flex-col gap-1.5 text-sm">
        目标主页或 UID
        <Input
          value={value.uid}
          placeholder="https://weibo.com/u/5896401674 或数字 UID"
          readOnly={uidLocked}
          disabled={disabled || uidLocked}
          onChange={(e) => onChange({ ...value, uid: e.target.value })}
        />
      </label>
      <p className="text-muted-foreground text-xs leading-relaxed">
        {uidLocked
          ? "此来源已绑定目标，不能更换 UID。请停用旧来源，再新建来源采集其他作者，避免混合历史资料。"
          : "仅接受数字 UID 或微博官方 /u/UID 主页，保存时由服务端规范化；留空可仅用 JSONL 导入。已有资料首次绑定会校验作者归属。"}
        登录会话不是采集目标；来源身份由管理员选择，不按名称或登录身份推断。
      </p>
      <label className="flex flex-col gap-1.5 text-sm">
        采集模式
        <SimpleSelect value={value.mode ?? "legacy"} disabled={disabled}
          onChange={(mode) => {
            if (mode === "legacy") { const next = { ...value }; delete next.mode; onChange(next); }
            else onChange({ ...value, mode: mode as WeiboCollectorConfig["mode"] });
          }}
          options={[
            { value: "legacy", label: "保留旧行为（最近窗口）" },
            { value: "recent", label: "最近窗口采集" },
            { value: "full_then_incremental", label: "全部历史 → 核验后增量" },
          ]} />
      </label>
      {value.mode === "full_then_incremental" ? <p className="text-warning text-xs leading-relaxed">
        遍历全部可访问历史，分批继续，不以首次天数或单批完成作为历史终点。先完成帖子、讨论、索引与完整性核验，再开启定时增量；资源不足会保留进度并阻塞。
      </p> : null}
      <label className="flex items-center justify-between gap-3 text-sm">
        包含公开评论
        <Switch checked={!!value.include_comments} disabled={disabled} aria-label="包含公开评论"
          onCheckedChange={(include_comments) => onChange({ ...value, include_comments, ...(include_comments ? {} : { include_replies: false }) })} />
      </label>
      <label className="flex items-center justify-between gap-3 text-sm">
        包含楼中楼回复
        <Switch checked={!!value.include_replies} disabled={disabled} aria-label="包含楼中楼回复"
          onCheckedChange={(include_replies) => onChange({ ...value, include_replies, ...(include_replies ? { include_comments: true } : {}) })} />
      </label>
      <p className="text-muted-foreground text-xs leading-relaxed">
        评论及楼中楼仅支持「全部历史 → 核验后增量」模式；最近窗口不支持。请显式选择模式，界面不会自动扩大采集范围。
      </p>
      {value.include_comments ? <label className="flex flex-col gap-1.5 text-sm">
        社区讨论分区 ID
        <Input value={value.discussion_source_id ?? ""} disabled={disabled} maxLength={32}
          placeholder="weibo_yys_comments" onChange={(e) => onChange({ ...value, discussion_source_id: e.target.value.trim() })} />
        <span className="text-muted-foreground text-xs">保存时创建或校验关联社区分区；不会覆盖同名来源。评论按真实作者保存，与官方正文隔离，由主来源统一采集。</span>
      </label> : null}
      <div className="grid grid-cols-2 gap-3">
        {COLLECTOR_NUMBER_FIELDS.filter(({ key }) => value.mode !== "full_then_incremental" || key !== "initial_days").map(({ key, label, min, max }) => (
          <label key={key} className="flex flex-col gap-1.5 text-xs">
            {value.mode === "full_then_incremental" && key === "interval_seconds" ? "历史完成后的增量周期（秒）" : key === "max_posts" ? "每个有界批次最多帖子" : label}
            <Input type="number" min={min} max={max} step={1}
              value={Number.isFinite(value[key]) ? value[key] : ""}
              onChange={(e) => onChange({ ...value, [key]: e.target.value === "" ? NaN : Number(e.target.value) })}
            />
            <span className="text-muted-foreground">{min}–{max}</span>
          </label>
        ))}
      </div>
      <label className="flex items-center justify-between gap-3 text-sm">
        包含该账号公开转发
        <Switch checked={value.include_reposts} disabled={disabled}
          onCheckedChange={(include_reposts) => onChange({ ...value, include_reposts })} />
      </label>
      <label className="flex items-center justify-between gap-3 text-sm">
        开启定时采集
        <Switch checked={value.enabled} disabled={disabled}
          onCheckedChange={(enabled) => onChange({ ...value, enabled })} />
      </label>
      <p className={cn("text-xs leading-relaxed", value.enabled ? "text-warning" : "text-muted-foreground")}>
        {value.enabled
          ? "保存开启定时后会自动启动首轮；达到单轮数量/时间上限会保留断点，不代表完整历史。"
          : "定时已暂停；配置目标后仍可手动立即采集。参与回答的挂载开关与定时开关独立。"}
      </p>
      {problem ? <p role="alert" className="text-destructive text-xs">{problem}</p> : null}
    </fieldset>
  );
}

function CreateSourceDialog({
  open,
  takenIds,
  busy,
  onClose,
  onCreate,
}: {
  open: boolean;
  takenIds: string[];
  busy: boolean;
  onClose: () => void;
  onCreate: (id: string, label: string, role: SourceInfo["role"], collector: WeiboCollectorConfig | null) => void;
}) {
  const [id, setId] = useState("");
  const [label, setLabel] = useState("");
  const [role, setRole] = useState<SourceInfo["role"]>("unknown");
  const [collector, setCollector] = useState<WeiboCollectorConfig>({ ...DEFAULT_COLLECTOR });

  useEffect(() => {
    if (open) { setId(""); setLabel(""); setRole("unknown"); setCollector({ ...DEFAULT_COLLECTOR }); }
  }, [open]);

  const idProblem = !id
    ? ""
    : !SOURCE_ID.test(id)
      ? "小写字母开头，只含小写字母、数字、下划线，最多 32 位"
      : takenIds.includes(id)
        ? "已有同名来源"
        : "";

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o && !busy) onClose(); }}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>新建微博来源</DialogTitle>
          <DialogDescription>
            为目标作者配置公开帖子采集，默认仅手动；也可继续使用标准资料 JSONL 导入。来源 ID 建好后不能更改。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="src-id">来源 ID</Label>
            <Input
              id="src-id"
              className="font-mono"
              placeholder="weibo_official"
              value={id}
              disabled={busy}
              aria-invalid={!!idProblem}
              onChange={(e) => setId(e.target.value.trim())}
            />
            <p className={cn("text-xs", idProblem ? "text-destructive" : "text-muted-foreground")}>
              {idProblem || "知识库目录名，建好后不要改。"}
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="src-label">显示名称</Label>
            <Input
              id="src-label"
              placeholder="YYS 官方微博"
              value={label}
              maxLength={40}
              disabled={busy}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="src-role">来源身份</Label>
            <SimpleSelect
              value={role}
              onChange={(v) => setRole(v as SourceInfo["role"])}
              disabled={busy}
              className="w-full"
              options={[
                { value: "unknown", label: "身份未知" },
                { value: "official", label: "官方" },
                { value: "community", label: "社区" },
              ]}
            />
            <p className="text-muted-foreground text-xs">
              影响回答里标出处的说法；不确定可先选「身份未知」。
            </p>
          </div>
          <CollectorFields value={collector} onChange={setCollector} disabled={busy} />
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>取消</Button>
          <Button
            loading={busy}
            disabled={!id || !!idProblem || !label.trim() || !!collectorProblem(collector, id)}
            onClick={() => {
              if (confirmSchedule(collector)) onCreate(id, label.trim(), role, collector.uid.trim() ? { ...collector, uid: collector.uid.trim() } : null);
            }}
          >
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const EXAMPLE_JSONL = `{"version":1,"id":"123456789","text":"测试资料正文","title":"示例标题","published_at":"2026-01-01T08:00:00+08:00","author_name":"示例作者"}
{"version":1,"id":"987654321","text":"另一条资料"}`;

type ImportStep = "input" | "previewing" | "preview" | "importing" | "done" | "error";

function ImportDialog({
  source,
  onClose,
  onDone,
}: {
  source: SourceInfo | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [step, setStep] = useState<ImportStep>("input");
  const [content, setContent] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [status, setStatus] = useState<ImportStatus | null>(null);
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    if (!source) {
      setStep("input"); setContent(""); setFileName(null);
      setPreview(null); setJob(null); setStatus(null); setError("");
      return;
    }
    // 打开时：若已有在途任务直接进轮询；已有终态显示结果
    const existing = source.import_status;
    if (existing) {
      const syntheticJob: ImportJob = {
        job_id: existing.job_id ?? "",
        state: existing.state,
        source_id: source.id,
        records: existing.records ?? 0,
        new: existing.new ?? 0,
        updated: existing.updated ?? 0,
        unchanged: existing.unchanged ?? 0,
        total_documents: 0,
        chunks: existing.chunks ?? 0,
        empty: false,
        sample: [],
      };
      setJob(syntheticJob);
      setStatus(existing);
      if (importIsActive(existing.state)) {
        setStep("importing");
      } else {
        setStep("done");
      }
    }
  }, [source]);

  // 轮询导入状态
  useEffect(() => {
    if (!source || !job || step !== "importing") return;
    pollRef.current = window.setInterval(async () => {
      try {
        const res = await api.importStatus(source.id);
        if (!res.import_status) return;
        setStatus(res.import_status);
        if (!importIsActive(res.import_status.state)) {
          window.clearInterval(pollRef.current ?? undefined);
          setStep("done");
          onDone();
        }
      } catch {
        // 忽略轮询错误，继续等
      }
    }, 2000);
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [source, job, step, onDone]);

  const MAX_BYTES = 10 * 1024 * 1024; // 10 MiB，与后端一致

  const handleFile = (file: File) => {
    if (file.size > MAX_BYTES) {
      setError("文件过大，请分批上传（单次限 10 MiB）");
      return;
    }
    setFileName(file.name);
    const reader = new FileReader();
    reader.onload = (e) => setContent(String(e.target?.result ?? ""));
    reader.readAsText(file, "utf-8");
  };

  // 粘贴内容的 UTF-8 字节数（TextEncoder 精确计数）
  const contentBytes = content ? new TextEncoder().encode(content).length : 0;
  const contentOverLimit = contentBytes > MAX_BYTES;

  const doPreview = async () => {
    if (!source || !content.trim()) return;
    setStep("previewing"); setError("");
    try {
      const res = await api.previewImport(source.id, content);
      setPreview(res); setStep("preview");
    } catch (e) {
      setError((e as Error).message); setStep("error");
    }
  };

  const doImport = async () => {
    if (!source || !content.trim()) return;
    setStep("importing"); setError("");
    try {
      const res = await api.startImport(source.id, content);
      setJob(res);
      setStatus({ state: res.state });
    } catch (e) {
      setError((e as Error).message); setStep("error");
    }
  };

  const doCancel = async () => {
    if (!source) return;
    try {
      const res = await api.cancelImport(source.id);
      if (res.cancelled) {
        toast.success("已请求取消");
      } else {
        toast.info("已进入提交阶段或无在途任务，未取消");
      }
    } catch (e) {
      toast.error("取消失败", { description: (e as Error).message });
    }
  };

  const open = source !== null;

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>导入资料 · {source?.label ?? ""}</DialogTitle>
          <DialogDescription>
            上传标准资料 JSONL，每行一条记录。预览后确认导入，不会替换已有记录，只追加/更新。
          </DialogDescription>
        </DialogHeader>

        {/* 格式说明 */}
        {step === "input" ? (
          <div className="flex flex-col gap-3">
            <div className="rounded-md border bg-muted/40 px-3 py-2.5 text-xs leading-relaxed">
              <p className="font-medium mb-1">JSONL 格式（UTF-8，每行一个 JSON 对象）</p>
              <p className="text-muted-foreground mb-1">
                必填：<code>version</code>（固定为 1）、<code>id</code>（微博原生数字帖 ID，1–32 位 ASCII 数字，不接受 URL 中的 base62 别名）、<code>text</code>（正文）。<br />
                可选：<code>title</code>、<code>url</code>（原文链接，仅作引用）、<code>published_at</code>（带时区 ISO 8601）、<code>author_id</code>、<code>author_name</code>。<br />
                禁止：<code>source_id</code>、<code>wiki</code>、<code>path</code> 等归属字段（后端会拒绝）。
              </p>
              <details>
                <summary className="cursor-pointer text-muted-foreground hover:text-foreground">示例（点击展开）</summary>
                <Pre className="mt-1.5 text-[11px]">{EXAMPLE_JSONL}</Pre>
              </details>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label>选择文件或直接粘贴内容</Label>
              <div className="flex gap-2">
                <input
                  ref={fileRef}
                  type="file"
                  accept=".jsonl,.ndjson,.json,.txt"
                  className="hidden"
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }}
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => fileRef.current?.click()}
                >
                  <Upload className="size-3.5" />
                  {fileName ? fileName : "选择文件"}
                </Button>
                {fileName ? (
                  <Button variant="ghost" size="sm" onClick={() => { setFileName(null); setContent(""); }}>
                    清除
                  </Button>
                ) : null}
              </div>
              <textarea
                className="bg-background border rounded-md p-2.5 font-mono text-xs h-36 resize-y focus:outline-none focus:ring-1 focus:ring-ring"
                placeholder={"粘贴 JSONL 内容，或从上方选文件自动填入…"}
                value={content}
                onChange={(e) => { setContent(e.target.value); setFileName(null); }}
              />
              <p className={cn("text-xs", contentOverLimit ? "text-destructive" : "text-muted-foreground")}>
                {content.trim()
                  ? `${content.trim().split("\n").length} 行 · ${(contentBytes / 1024).toFixed(1)} KiB${contentOverLimit ? "（超过 10 MiB 限制，请分批上传）" : ""}`
                  : "还没有内容"}
              </p>
            </div>
          </div>
        ) : step === "previewing" ? (
          <div className="flex items-center gap-2 py-6 justify-center text-sm text-muted-foreground">
            <Spinner /> 校验中，请稍候…
          </div>
        ) : step === "preview" && preview ? (
          <div className="flex flex-col gap-3">
            <PreviewStats data={preview} />
            {preview.sample.length > 0 ? (
              <div className="rounded-md border">
                <div className="text-muted-foreground px-3 py-1.5 text-xs font-medium border-b">
                  前 {preview.sample.length} 条样本预览
                </div>
                <div className="max-h-52 overflow-y-auto px-3">
                  {preview.sample.map((item, i) => (
                    <SampleRow key={i} item={item as Record<string, unknown>} />
                  ))}
                </div>
              </div>
            ) : null}
            {preview.records === 0 ? (
              <Banner tone="warning" className="mb-0">没有可导入的记录。</Banner>
            ) : null}
          </div>
        ) : step === "importing" || step === "done" ? (
          <div className="flex flex-col gap-3">
            {job ? <PreviewStats data={job} /> : null}
            <div className="flex items-center gap-2">
              {status ? <ImportBadge status={status} /> : <Spinner />}
              {status?.message ? (
                <span className="text-xs text-muted-foreground">{status.message}</span>
              ) : null}
            </div>
            {status?.state === "completed" ? (
              <div className="flex items-center gap-1.5 text-sm text-success">
                <CheckCircle className="size-4" /> 导入完成
                {status.chunks != null ? `，共写入 ${fmtCount(status.chunks)} 个切片` : ""}
              </div>
            ) : null}
            {status?.state === "failed" ? (
              <div className="flex items-start gap-1.5 text-sm text-destructive">
                <XCircle className="size-4 mt-0.5 shrink-0" />
                <span>{status.error || "导入失败，请重新导入。"}</span>
              </div>
            ) : null}
            {status?.state === "recovery_required" ? (
              <div className="flex items-start gap-1.5 text-sm text-warning">
                <AlertTriangle className="size-4 mt-0.5 shrink-0" />
                <span>{status.error || "完整快照已保留，需恢复后再导入。"}</span>
              </div>
            ) : null}
            {status?.state === "cancelled" ? (
              <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
                <XCircle className="size-4" /> 已取消，原有资料未受影响。
              </div>
            ) : null}
          </div>
        ) : step === "error" ? (
          <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/8 p-3 text-sm text-destructive">
            <AlertTriangle className="size-4 mt-0.5 shrink-0" />
            <span>{error || "未知错误"}</span>
          </div>
        ) : null}

        <DialogFooter>
          {step === "importing" && status && importIsActive(status.state) ? (
            <>
              <Button variant="outline" onClick={() => void doCancel()}>请求取消</Button>
              <Button variant="outline" onClick={onClose}>后台继续，关闭</Button>
            </>
          ) : null}
          {step === "input" || step === "error" ? (
            <>
              <Button variant="outline" onClick={onClose}>取消</Button>
              <Button
                disabled={!content.trim() || contentOverLimit}
                onClick={() => void doPreview()}
              >
                预览
              </Button>
            </>
          ) : step === "previewing" ? (
            <Button variant="outline" disabled>预览中…</Button>
          ) : step === "preview" ? (
            <>
              <Button variant="outline" onClick={() => setStep("input")}>返回修改</Button>
              <Button
                disabled={!preview || preview.records === 0}
                onClick={() => void doImport()}
              >
                确认导入
              </Button>
            </>
          ) : step === "done" || (step === "importing" && status && !importIsActive(status.state)) ? (
            <>
              <Button
                variant="outline"
                onClick={() => {
                  setStep("input");
                  setContent("");
                  setFileName(null);
                  setJob(null);
                  setStatus(null);
                  setPreview(null);
                }}
              >
                导入新批次
              </Button>
              <Button onClick={onClose}>关闭</Button>
            </>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

const COLLECTOR_STATES: Record<string, { label: string; tone: "muted" | "info" | "success" | "danger" | "warning" }> = {
  not_configured: { label: "未配置目标", tone: "muted" },
  paused: { label: "定时暂停", tone: "muted" },
  idle: { label: "等待采集", tone: "muted" },
  queued: { label: "已排队", tone: "info" },
  fetching: { label: "采集中", tone: "info" },
  collecting: { label: "采集中", tone: "info" },
  importing: { label: "导入中", tone: "info" },
  committing: { label: "提交中", tone: "info" },
  stopping: { label: "正在停止", tone: "warning" },
  completed: { label: "采集完成", tone: "success" },
  failed: { label: "采集失败", tone: "danger" },
  cancelled: { label: "本次已取消", tone: "muted" },
  interrupted: { label: "采集已中断", tone: "warning" },
  needs_login: { label: "需要重新同步登录", tone: "warning" },
  limited: { label: "采集范围受限", tone: "warning" },
  blocked: { label: "采集受阻", tone: "warning" },
  indexing: { label: "索引中", tone: "info" },
  verifying: { label: "核验中", tone: "info" },
};

const COLLECTION_PHASE_LABELS: Record<string, string> = {
  queued: "等待后台执行", fetching: "获取公开帖子", collecting: "获取公开帖子",
  normalizing: "规范化帖子", validating: "校验资料", embedding: "生成向量",
  importing: "增量导入", committing: "安全提交", stopping: "安全停止",
  completed: "已结束", failed: "已结束", cancelled: "已取消",
};

function collectionIsActive(state: string): boolean {
  return ["queued", "fetching", "collecting", "importing", "committing", "indexing", "verifying", "stopping"].includes(state);
}

function collectionTime(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

const HISTORY_PHASE_LABELS: Record<string, string> = {
  posts: "历史帖子采集中", comments: "历史评论 / 楼中楼采集中", indexing: "历史资料索引中",
  verifying: "历史完整性核验中", incremental: "增量采集中", paused: "历史采集已暂停", blocked: "历史采集受阻",
};

export function historyIsIncremental(status?: CollectorStatus | null): boolean {
  return status?.history_complete === true && status.history_phase === "incremental";
}

function measuredCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? fmtCount(value) : "—";
}

function measuredBytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB`
    : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(2)} MiB` : `${fmtCount(value)} B`;
}

/** 只使用服务端累计提交/核验结果，不从本批统计或规模估计推导百分比。 */
export function HistoryProgress({ status, intervalSeconds }: { status?: CollectorStatus | null; intervalSeconds: number }) {
  const incremental = historyIsIncremental(status);
  const phase = status?.history_phase;
  const measurement = Object.entries(status?.measurement ?? {}).filter(([, value]) =>
    typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value)),
  ).slice(0, 24);
  return <section aria-label="全历史采集进度" className="flex flex-col gap-2 rounded-lg border p-3">
    <p className={incremental ? "text-success font-medium" : "text-warning font-medium"}>
      {incremental ? `全历史已核验完成 · ${intervalSeconds === 1800 ? "每 30 分钟" : `每 ${intervalSeconds} 秒`}增量`
        : status?.history_complete === true ? "历史已核验 · 当前尚未进入增量" : "历史尚未完成 · 尚未进入增量"}
    </p>
    <p>历史阶段：{status?.paused ? "历史采集已暂停" : phase === "incremental" && !incremental ? "状态待核实（尚无历史完成证据）" : phase && Object.hasOwn(HISTORY_PHASE_LABELS, phase) ? HISTORY_PHASE_LABELS[phase] : "等待服务端报告历史阶段"}</p>
    {!phase ? <p className="text-muted-foreground">尚无新版历史状态；旧窗口或本批完成记录不能作为全历史完成证据。</p> : null}
    <div className="grid grid-cols-1 gap-1 sm:grid-cols-3">
      <span>累计提交历史帖子 <strong>{measuredCount(status?.posts_total)}</strong></span>
      <span>累计提交评论 <strong>{measuredCount(status?.comments_total)}</strong></span>
      <span>累计提交楼中楼 <strong>{measuredCount(status?.replies_total)}</strong></span>
    </div>
    <p className="text-muted-foreground">已保存时间范围：{collectionTime(status?.oldest_date)} → 快照上界 {collectionTime(status?.snapshot_at)}</p>
    <p className="text-muted-foreground">历史开始：{collectionTime(status?.history_started_at)} · 下批运行：{collectionTime(status?.next_batch_at)}</p>
    <div className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1">
      <span>待处理帖子 {measuredCount(status?.pending_posts)}</span><span>待处理评论 {measuredCount(status?.pending_comments)}</span>
      <span>待处理楼中楼 {measuredCount(status?.pending_replies)}</span><span>待索引 {measuredCount(status?.index_pending)}</span>
      <span>无法访问 / 未能访问 {measuredCount(status?.inaccessible)}</span><span>数量差异 {measuredCount(status?.count_mismatches)}</span>
    </div>
    <p className="text-muted-foreground">资源占用 {measuredBytes(status?.resource_bytes)} · 向量占用 {measuredBytes(status?.vector_bytes)}（缺失数据以 — 表示）</p>
    {status?.coverage_message ? <p className={phase === "blocked" ? "text-warning" : ""}>覆盖 / 索引 / 资源说明：{status.coverage_message}</p> : null}
    {phase === "blocked" ? <p className="text-warning">历史仍未完成，已保留断点；请根据服务端阻塞说明处理，不会提前转增量。{!status?.coverage_message && !status?.message ? "阻塞原因尚未返回，请刷新确认。" : ""}</p> : null}
    {measurement.length ? <details><summary className="cursor-pointer">服务端有界测量结果</summary>
      <dl className="mt-1 grid grid-cols-1 gap-1 text-muted-foreground sm:grid-cols-2">{measurement.map(([key, value]) => <div key={key} className="break-words"><dt className="inline">{key.slice(0, 100)}：</dt><dd className="inline">{String(value).slice(0, 500)}</dd></div>)}</dl>
    </details> : <p className="text-muted-foreground">测量结果尚未返回；不据此估算全量或完成时间。</p>}
  </section>;
}

/** 独立轮询当前任务；取消始终绑定点击时的 job_id，不重试取消新任务。 */
function CollectorLine({ source }: { source: SourceInfo }) {
  const client = useQueryClient();
  const key = ["source-collector", source.id];
  const query = useQuery({
    queryKey: key,
    queryFn: () => api.sourceCollector(source.id),
    initialData: source.collection ?? undefined,
    retry: false,
    refetchInterval: (current) => {
      const error = current.state.error;
      if (error instanceof ApiError && [400, 403, 404, 409].includes(error.status)) return false;
      return collectionIsActive(current.state.data?.state ?? source.collector_status) ? 2500 : 30_000;
    },
  });
  const status = query.data ?? source.collection;
  const state = status?.state ?? source.collector_status;
  const fullMode = source.collector?.mode === "full_then_incremental";
  const incremental = fullMode && historyIsIncremental(status);
  const historyPaused = status?.paused === true || status?.history_phase === "paused";
  const knownState = Object.hasOwn(COLLECTOR_STATES, state);
  const batchPresentation = knownState ? COLLECTOR_STATES[state] : { label: "未知状态，先刷新确认", tone: "warning" as const };
  const presentation = fullMode && historyPaused ? { label: "历史采集已暂停", tone: "muted" as const }
    : fullMode && state === "completed" && !incremental
      ? { label: status?.history_complete ? "本批完成 · 等待增量阶段" : "本批完成 · 历史未完成", tone: "info" as const }
      : batchPresentation;
  const active = collectionIsActive(state);
  const committing = state === "committing" || status?.phase === "committing";
  const configured = !!source.collector;
  const imported = !!source.import_status && importIsActive(source.import_status.state);
  const refresh = () => {
    void client.invalidateQueries({ queryKey: key });
    void client.invalidateQueries({ queryKey: ["sources"] });
  };
  const saveStatus = (value: CollectorStatus) => {
    client.setQueryData(key, value);
    void client.invalidateQueries({ queryKey: ["sources"] });
  };
  const collect = useMutation({
    mutationFn: () => api.collectSource(source.id),
    retry: false,
    onMutate: () => client.cancelQueries({ queryKey: key }),
    onSuccess: async (value) => {
      await client.cancelQueries({ queryKey: key });
      saveStatus(value);
      toast.message("采集请求已提交", { description: "由后台采集进程执行，请关注当前任务状态。" });
    },
    onError: (error: Error) => {
      toast.error("未能启动采集", { description: error.message });
      refresh();
    },
    onSettled: () => void client.invalidateQueries({ queryKey: key }),
  });
  const cancel = useMutation({
    mutationFn: (jobId: string) => api.cancelSourceCollection(source.id, jobId),
    retry: false,
    onMutate: () => client.cancelQueries({ queryKey: key }),
    onSuccess: async (value, jobId) => {
      await client.cancelQueries({ queryKey: key });
      if (value.job_id !== jobId) {
        toast.warning("任务已变化，未继续停止操作，请确认最新任务");
        refresh();
        return;
      }
      saveStatus(value);
      // 接受停止请求不代表已经取消，更不能当作采集成功。
      toast.message(fullMode && !incremental
        ? value.paused || value.history_phase === "paused" ? "历史采集已持久暂停，可继续断点采集" : "历史暂停请求已提交，请等待安全保存断点"
        : value.state === "cancelled" ? "本次任务已取消" : "停止请求已提交，请等待任务安全结束");
    },
    onError: (error: Error) => {
      toast.error(error instanceof ApiError && error.status === 409
        ? "任务已变化或进入提交阶段，停止请求未继续；请刷新确认"
        : "停止请求失败", { description: error.message });
      refresh(); // 只读取最新状态；绝不自动用新 job_id 再次取消。
    },
    onSettled: () => void client.invalidateQueries({ queryKey: key }),
  });
  const busy = collect.isPending || cancel.isPending;
  const canStart = configured && source.mounted && !active && !committing && !imported && knownState
    && !query.isError && !query.isPending;
  const retry = ["failed", "interrupted", "cancelled", "needs_login", "limited"].includes(state);
  // 仅从规范数字 UID 构造目标链接，不渲染用户提交的任意 URL。
  const uid = source.collector?.uid ?? status?.uid;
  const profileUrl = uid && /^\d+$/.test(uid) ? `https://weibo.com/u/${uid}` : null;
  return (
    <div className="flex flex-col gap-2 rounded-lg bg-muted/40 p-3 text-xs" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={presentation.tone}>
          {active ? <Loader2 className="mr-1 size-3 animate-spin" /> : null}{presentation.label}
        </Badge>
        <span className="text-muted-foreground">
          {configured ? fullMode && !incremental ? (historyPaused ? "断点已保留 · 等待继续" : "历史阶段 · 按有界批次续采") : (source.collector?.enabled ? "定时开启" : "定时暂停 · 可手动采集") : "请在采集设置中填写目标；仍可导入 JSONL"}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button size="xs" variant="outline" disabled={busy || !canStart} loading={collect.isPending}
            onClick={() => collect.mutate()}>{fullMode && !incremental ? "继续采集" : retry ? "重试采集" : "立即采集"}</Button>
          {active || (fullMode && !incremental && !historyPaused) ? (
            <Button size="xs" variant="outline" loading={cancel.isPending}
              disabled={busy || committing || state === "stopping" || !status?.job_id || query.isError}
              onClick={() => {
                const jobId = status?.job_id;
                const question = fullMode && !incremental
                  ? "暂停历史采集？断点会持久保留，后台不会自动续批；点击继续采集后从断点恢复。"
                  : "停止当前这次采集？未提交内容不会计作成功；定时开关不会改变。";
                if (jobId && !committing && window.confirm(question)) cancel.mutate(jobId);
              }}>{fullMode && !incremental ? "暂停历史" : "停止本次"}</Button>
          ) : null}
          <Button size="xs" variant="ghost" disabled={query.isFetching || busy}
            onClick={() => void query.refetch()} aria-label="刷新采集状态"><RefreshCw className="size-3.5" /></Button>
        </div>
      </div>
      {profileUrl ? (
        <a href={profileUrl} target="_blank" rel="noopener noreferrer" className="inline-flex w-fit items-center gap-1 underline underline-offset-2">
          目标 UID {uid}<ExternalLink className="size-3" />
        </a>
      ) : null}
      {source.collector ? <p className="text-muted-foreground">
        {fullMode ? "全部可访问历史（不按天数截断）" : `首轮 ${source.collector.initial_days} 天 · 回扫 ${source.collector.overlap_days} 天`} · 每批最多 {source.collector.max_posts} 条 · {fullMode ? "完成核验后增量" : `周期 ${source.collector.interval_seconds} 秒`} · {source.collector.include_reposts ? "含公开转发" : "仅原创"}
        {source.collector.include_comments ? ` · 评论 → 社区分区 ${source.collector.discussion_source_id ?? "未配置"}${source.collector.include_replies ? "（含楼中楼）" : "（不含楼中楼）"}` : " · 不含评论"}
      </p> : null}
      {fullMode ? <HistoryProgress status={status} intervalSeconds={source.collector?.interval_seconds ?? 1800} /> : null}
      {status ? <>
        {status.phase ? <p className="text-muted-foreground">{fullMode ? "本批阶段" : "阶段"}：{Object.hasOwn(COLLECTION_PHASE_LABELS, status.phase) ? COLLECTION_PHASE_LABELS[status.phase] : "未知阶段，请以任务状态与说明为准"}</p> : null}
        <div className="text-muted-foreground flex flex-wrap gap-x-3 gap-y-1">
          {fullMode ? <span>以下仅为本批：</span> : null}<span>页数 {status.pages ?? 0}</span><span>获取 {status.fetched ?? 0}</span><span>接纳 {status.accepted ?? 0}</span>
          <span>新增 {status.new ?? 0}</span><span>更新 {status.updated ?? 0}</span><span>未变 {status.unchanged ?? 0}</span>
        </div>
        <div className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
          <span>本次开始 {collectionTime(status.started_at)}</span>
          <span>本次结束 {collectionTime(status.finished_at)}</span>
          <span>最近成功 {collectionTime(status.last_success_at)}</span>
          {!fullMode || incremental ? <span>下次增量运行 {collectionTime(status.next_run_at)}</span> : null}
        </div>
        {status.message ? <p>{status.message}</p> : null}
        {status.limited || state === "limited" ? <p className="text-warning">本轮范围或容量受限，不代表完整历史；保留断点与已有资料，请检查状态说明及限额后继续。</p> : null}
        {state === "needs_login" || status.error_code === "authentication_required" ? (
          <p className="text-warning">请在本机重新同步微博登录会话，再刷新状态并重试。共享登录身份与目标作者独立，无需更改目标 UID。</p>
        ) : null}
        {status.error_code ? <p className="text-muted-foreground">错误代码：{status.error_code}</p> : null}
      </> : null}
      {committing ? <p className="text-warning">正在安全提交，暂不能停止；请等待最终状态，勿把尚未完成的提交当作取消成功。</p> : null}
      {!source.mounted ? <p className="text-warning">来源未挂载：不参与回答且不会开始新采集；已有资料保留。恢复挂载与开启定时是两项独立设置。</p> : null}
      {query.isError ? <p role="alert" className="text-destructive">采集状态读取失败：{query.error.message}。请刷新确认，当前不发起新任务。</p> : null}
    </div>
  );
}

/** 导入状态简要行（卡片底部） */
function ImportStatusLine({
  status,
  onOpenImport,
  onRecover,
}: {
  status: ImportStatus | null;
  onOpenImport: () => void;
  onRecover: () => void;
}) {
  if (!status) {
    return (
      <div className="text-muted-foreground flex items-center gap-2 text-xs">
        <span>还没有导入记录。</span>
        <Button variant="outline" size="xs" onClick={onOpenImport}>
          <Upload className="size-3.5" /> 导入资料
        </Button>
      </div>
    );
  }
  const active = importIsActive(status.state);
  const isRecovery = status.state === "recovery_required";
  const isInterrupted = status.state === "interrupted";
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <ImportBadge status={status} />
        {status.message ? (
          <span className="text-muted-foreground text-xs">{status.message}</span>
        ) : null}
        {status.finished_at ? (
          <span className="text-muted-foreground text-xs">{formatAgo(status.finished_at)}</span>
        ) : null}
        {/* recovery_required：恢复按钮；interrupted/完成/失败/取消：再次导入 */}
        {isRecovery ? (
          <Hint text="将已保留的完整快照写入知识库，不新增也不清空资料。恢复完成后即可正常导入。">
            <Button
              variant="outline"
              size="xs"
              onClick={() => {
                if (window.confirm("恢复导入状态：将已保留的完整快照写入知识库，不会新增或清空资料。确认继续？")) {
                  onRecover();
                }
              }}
            >
              恢复导入状态
            </Button>
          </Hint>
        ) : !active ? (
          <Button variant="outline" size="xs" onClick={onOpenImport}>
            <Upload className="size-3.5" />
            {isInterrupted ? "重新导入" : "再次导入"}
          </Button>
        ) : null}
      </div>
      {isRecovery ? (
        <p className="text-warning text-xs">
          完整快照已保留，需恢复后再导入。恢复期间无法发起新的导入。
        </p>
      ) : isInterrupted ? (
        <p className="text-muted-foreground text-xs">
          进程重启导致任务中断，原有资料未改变，可重新上传导入。
        </p>
      ) : null}
    </div>
  );
}

/** 微博来源卡片 */
export function WeiboSourceCard({
  source,
  locked,
  onToggleMounted,
  onOpenImport,
  onRecover,
  onEdit,
}: {
  source: SourceInfo;
  locked: boolean;
  onToggleMounted: (mounted: boolean) => void;
  onOpenImport: () => void;
  onRecover: () => void;
  onEdit: () => void;
}) {
  const kb = source.knowledge;
  return (
    <Card className="flex flex-col gap-2.5 p-4">
      {/* 标题行 */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="text-sm font-semibold">{source.label}</span>
        <code className="font-mono text-xs text-muted-foreground">{source.id}</code>
        <Badge tone={ROLE_TONES[source.role]}>{ROLE_LABELS[source.role]}</Badge>
        <Badge tone="outline">{source.platform === "weibo" ? "微博" : source.platform}</Badge>
        {!source.mounted ? <Badge tone="warning">已停用</Badge> : null}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="xs" onClick={onEdit} disabled={locked}>
            {source.platform === "weibo" && !source.discussion_of ? "采集设置" : "编辑"}
          </Button>
          <label className="flex items-center gap-1.5">
            <span className="text-muted-foreground text-xs">参与回答</span>
            <Switch
              checked={source.mounted}
              disabled={locked}
              onCheckedChange={onToggleMounted}
              aria-label={`${source.label} 参与回答`}
            />
          </label>
        </div>
      </div>

      {/* 知识库数量 */}
      {kb ? (
        <div className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
          <span>
            文档 <strong className="text-foreground/80">{fmtCount(kb.documents)}</strong>
          </span>
          <span>
            切片 <strong className="text-foreground/80">{fmtCount(kb.chunks)}</strong>
          </span>
          {kb.latest_document_at ? (
            <span>最新资料 {formatAgo(kb.latest_document_at)}</span>
          ) : null}
          {!source.mounted ? (
            <span className="text-warning">已停用：不参与检索与回答，资料保留</span>
          ) : null}
        </div>
      ) : (
        <div className="text-muted-foreground text-xs">尚无资料</div>
      )}

      {/* 采集器状态 */}
      {source.platform === "weibo" && !source.discussion_of ? <CollectorLine source={source} /> : null}
      {source.platform === "weibo" && source.discussion_of ? <p className="text-muted-foreground text-xs">
        社区讨论分区 · 关联主来源 <code>{source.discussion_of}</code>。评论与楼中楼保留真实作者，不作为官方正文；由主来源统一采集、暂停和继续，不单独启动时间线采集器。
      </p> : null}

      {/* 导入状态 */}
      <ImportStatusLine status={source.import_status} onOpenImport={onOpenImport} onRecover={onRecover} />
    </Card>
  );
}

/** Wiki 来源卡片（简要展示，完整管理跳转 /wikis） */
function WikiSourceCard({ source }: { source: SourceInfo }) {
  const kb = source.knowledge;
  return (
    <Card className="flex flex-col gap-2 p-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="text-sm font-semibold">{source.label}</span>
        <code className="font-mono text-xs text-muted-foreground">{source.id}</code>
        <Badge tone="outline">Wiki</Badge>
        {!source.mounted ? <Badge tone="warning">已卸下</Badge> : <Badge tone="success">挂载中</Badge>}
        <div className="ml-auto">
          <Link to="/wikis">
            <Button variant="ghost" size="xs">
              <ExternalLink className="size-3.5" /> 管理
            </Button>
          </Link>
        </div>
      </div>
      {kb ? (
        <div className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
          <span>
            文档 <strong className="text-foreground/80">{fmtCount(kb.documents)}</strong>
          </span>
          <span>
            切片 <strong className="text-foreground/80">{fmtCount(kb.chunks)}</strong>
          </span>
          {kb.latest_document_at ? (
            <span>最新 {formatAgo(kb.latest_document_at)}</span>
          ) : null}
        </div>
      ) : (
        <div className="text-muted-foreground text-xs">尚无资料</div>
      )}
      <div className="text-muted-foreground text-xs">
        Wiki 来源由自动同步管理；导入、密钥、建库操作请前往 Wiki 页面。
      </div>
    </Card>
  );
}

/** 编辑微博来源（标签/身份） */
function EditSourceDialog({
  source,
  busy,
  onClose,
  onSave,
}: {
  source: SourceInfo | null;
  busy: boolean;
  onClose: () => void;
  onSave: (id: string, label: string, role: SourceInfo["role"], collector?: WeiboCollectorConfig | null) => void;
}) {
  const [label, setLabel] = useState("");
  const [role, setRole] = useState<SourceInfo["role"]>("unknown");
  const [collector, setCollector] = useState<WeiboCollectorConfig>({ ...DEFAULT_COLLECTOR });
  useEffect(() => {
    if (source) {
      setLabel(source.label); setRole(source.role);
      setCollector({ ...(source.collector ?? DEFAULT_COLLECTOR) });
    }
  }, [source]);
  return (
    <Dialog open={source !== null} onOpenChange={(o) => { if (!o && !busy) onClose(); }}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{source?.platform === "weibo" && !source.discussion_of ? "采集设置" : "编辑来源"} · {source?.id}</DialogTitle>
          <DialogDescription>来源 ID 和平台不能更改；身份由管理员指定，与登录身份无关。运行中修改请先停止本次任务，提交阶段需等待结束。</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="edit-label">显示名称</Label>
            <Input
              id="edit-label"
              value={label}
              maxLength={40}
              disabled={busy}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="edit-role">来源身份</Label>
            <SimpleSelect
              value={role}
              onChange={(v) => setRole(v as SourceInfo["role"])}
              disabled={busy || !!source?.discussion_of}
              className="w-full"
              options={[
                { value: "unknown", label: "身份未知" },
                { value: "official", label: "官方" },
                { value: "community", label: "社区" },
              ]}
            />
          </div>
          {source?.platform === "weibo" && !source.discussion_of ? (
            <CollectorFields value={collector} onChange={setCollector} disabled={busy} uidLocked={!!source.collector} />
          ) : source?.discussion_of ? <p className="text-muted-foreground text-xs">此社区讨论分区关联 {source.discussion_of}，身份固定为社区；范围与采集控制请在主来源中设置。</p> : null}
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>取消</Button>
          <Button
            loading={busy}
            disabled={!label.trim() || (source?.platform === "weibo" && !source.discussion_of && !!collectorProblem(collector, source.id))}
            onClick={() => {
              if (!source) return;
              if (source.platform !== "weibo" || source.discussion_of) { onSave(source.id, label.trim(), source.discussion_of ? "community" : role); return; }
              if (confirmSchedule(collector, source.collector)) {
                onSave(source.id, label.trim(), role, collector.uid.trim() ? { ...collector, uid: collector.uid.trim() } : null);
              }
            }}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- 微博平台共享采集会话登录卡片 ----
// 此处是访问会话，与目标 UID、管理员指定的 source.role 均无关。
// 优先沿用本机同步；二维码仅是备用入口，valid=null 不代表失效。

function PlatformLoginCard({ hasWeibo }: { hasWeibo: boolean }) {
  const [qrOpen, setQrOpen] = useState(false);
  const [qrImage, setQrImage] = useState<string | null>(null);
  const [qrState, setQrState] = useState("");
  const [qrError, setQrError] = useState("");
  const [qrBusy, setQrBusy] = useState(false);
  const [clearBusy, setClearBusy] = useState(false);
  const [qrLoginId, setQrLoginId] = useState<string | null>(null);
  const loginIdRef = useRef<string | null>(null);

  const statusQuery = useQuery({
    queryKey: ["weibo-session"],
    queryFn: api.weiboSessionStatus,
    enabled: hasWeibo,
    refetchInterval: 60_000,
  });

  const status: WeiboSessionStatus | undefined = statusQuery.data;

  const refreshStatus = statusQuery.refetch;

  // 串行轮询且绑定本地 ID；旧弹窗的迟到响应不能更新或取消另一个登录。
  useEffect(() => {
    if (!qrOpen || !qrImage || !qrLoginId) return;
    let stopped = false;
    let timer: number | undefined;
    const current = () => !stopped && loginIdRef.current === qrLoginId;
    const poll = async () => {
      if (!current()) return;
      try {
        const res = await api.weiboSessionPoll(qrLoginId);
        if (!current()) return;
        if (res.login_id !== qrLoginId) {
          setQrState("cancelled");
          setQrError("登录会话已变化，请关闭弹窗后刷新页面");
          toast.error("登录会话已变化，请关闭弹窗后刷新页面");
          return;
        }
        setQrState(res.state);
        setQrError(res.error || "");
        if (res.state === "confirmed") {
          setQrOpen(false);
          setQrImage(null);
          setQrLoginId(null);
          loginIdRef.current = null;
          void refreshStatus();
          if (res.verified && res.valid === true) {
            toast.success("微博登录已验证；采集目标请在各来源的「采集设置」中管理");
          } else if (res.method === "browser") {
            toast.success("会话已保存，采集可用性待验证");
          } else {
            toast.warning("微博登录尚未验证，请刷新状态后重试");
          }
          return;
        }
        if (["error", "expired", "cancelled"].includes(res.state)) {
          toast.error(res.error || "二维码已失效，请重新生成");
          return;
        }
      } catch (e) {
        if (!current()) return;
        if (e instanceof ApiError && [400, 401, 403, 409].includes(e.status)) {
          setQrState("cancelled");
          setQrError(e.message);
          toast.error(e.message);
          return; // 仅停止自己的轮询，绝不无 ID 取消全局会话。
        }
        // 暂时网络故障不等于过期或登录成功。
      }
      if (current()) timer = window.setTimeout(() => void poll(), 2500);
    };
    timer = window.setTimeout(() => void poll(), 2500);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [qrOpen, qrImage, qrLoginId, refreshStatus]);

  const startQr = async () => {
    setQrError("");
    setQrBusy(true);
    try {
      const res = await api.weiboSessionQr();
      if (!res.available || !res.login_id) {
        toast.error("微信扫码登录组件暂不可用，请刷新页面后重试");
        return;
      }
      loginIdRef.current = res.login_id;
      setQrLoginId(res.login_id);
      setQrImage(res.image);
      setQrState(res.state);
      setQrOpen(true);
    } catch (e) {
      toast.error("二维码生成失败", { description: (e as Error).message });
    } finally {
      setQrBusy(false);
    }
  };

  const cancelQr = async () => {
    const loginId = loginIdRef.current;
    loginIdRef.current = null;
    setQrLoginId(null);
    setQrOpen(false);
    setQrImage(null);
    if (!loginId) return;
    try {
      await api.weiboSessionCancel(loginId);
    } catch (e) {
      // 失配意味着别的标签页已有新会话，不再尝试取消它。
      if (!(e instanceof ApiError && e.status === 409)) {
        toast.error("取消未完成", { description: (e as Error).message });
      }
    }
    void refreshStatus();
  };

  const clearSession = async () => {
    if (!window.confirm("确认清除本地微博采集会话？清除后采集将不可用，已导入的资料不受影响。")) return;
    setClearBusy(true);
    try {
      await api.weiboSessionClear();
      void statusQuery.refetch();
      toast.success("本地采集会话已清除");
    } catch (e) {
      toast.error("清除失败", { description: (e as Error).message });
    } finally {
      setClearBusy(false);
    }
  };

  if (!hasWeibo) return null;

  const loginUnavailable = status?.available === false;
  const configured = status?.configured ?? false;
  const locallyUsable = status?.locally_usable ?? false;
  const sessionDescription = status?.valid === true
    ? "微博登录已验证；采集目标请在各来源的「采集设置」中管理"
    : !configured
      ? "未配置采集会话"
      : locallyUsable
        ? "会话已保存，采集可用性待验证"
        : "本地登录会话不可读取，请在本机重新同步";

  return (
    <Card className="flex flex-col gap-0 overflow-hidden p-0">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-4 py-3">
        <div>
          <div className="text-sm font-semibold">微博登录会话（共享）</div>
          <div className="text-muted-foreground mt-0.5 max-w-2xl text-xs leading-relaxed">
            所有微博来源共用访问时的登录会话；被采集的目标作者与管理员指定的来源身份均独立配置。
            本机同步的会话即使显示「待验证」也不代表失效，不会因此禁止采集。实际提示需要登录时，请在本机重新同步，再重试采集。
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {configured ? (
            <Button
              variant="outline"
              size="xs"
              loading={clearBusy}
              disabled={clearBusy}
              onClick={() => void clearSession()}
            >
              清除本地会话
            </Button>
          ) : null}
          <Hint text={loginUnavailable ? "备用二维码暂不可用；可继续使用本机同步的登录会话" : "备用二维码入口，服务器扫码可用性以实际验证为准；无需为采集重新扫码"}>
            <span>
              <Button
                size="xs"
                variant="outline"
                loading={qrBusy}
                disabled={qrBusy || loginUnavailable || statusQuery.isPending}
                onClick={() => void startQr()}
              >
                <QrCode className="size-3.5" />
                备用微信扫码
              </Button>
            </span>
          </Hint>
        </div>
      </div>

      {/* 状态行 */}
      <div className="border-t px-4 py-2.5 text-xs">
        {statusQuery.isPending ? (
          <span className="text-muted-foreground flex items-center gap-1.5"><Spinner className="size-3" />读取状态中…</span>
        ) : statusQuery.isError ? (
          <span className="text-destructive">读取采集会话状态失败</span>
        ) : status ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span className={cn(
              "flex items-center gap-1.5",
              configured ? "text-warning" : "text-muted-foreground"
            )}>
              <span className={cn(
                "inline-block size-1.5 rounded-full shrink-0",
                configured ? "bg-warning" : "bg-muted-foreground/50"
              )} />
              {sessionDescription}
            </span>
            {["new", "generating", "waiting", "scanned"].includes(status.login) ? (
              <span className="text-muted-foreground">已有微信扫码登录进行中，请在原弹窗完成</span>
            ) : null}
            {status.updated_at ? (
              <span className="text-muted-foreground">
                更新于 {formatAgo(new Date(status.updated_at * 1000).toISOString())}
              </span>
            ) : null}
            {loginUnavailable ? (
              <span className="text-warning flex items-center gap-1">
                <AlertTriangle className="size-3" />
                {status.message || "微信扫码登录组件暂不可用"}
              </span>
            ) : null}
            {status.error ? (
              <span className="text-destructive">{status.error}</span>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* 二维码弹窗 */}
      <Dialog open={qrOpen} onOpenChange={(o) => { if (!o) void cancelQr(); }}>
        <DialogContent className="max-w-sm items-center">
          <DialogHeader>
            <DialogTitle>使用微信扫码登录微博</DialogTitle>
            <DialogDescription>
              这是备用登录入口，以服务端实际验证为准。此登录会话与目标作者无关；已有本机同步会话无需重新扫码。采集提示需要登录时优先在本机重新同步。
            </DialogDescription>
          </DialogHeader>
          {["expired", "error", "cancelled"].includes(qrState) ? (
            <div className="text-warning flex justify-center py-8"><AlertTriangle className="size-10" /></div>
          ) : qrImage ? (
            <img src={qrImage} alt="微信扫码登录微博的官方二维码" className="mx-auto size-56" />
          ) : (
            <div className="flex justify-center py-8"><Spinner className="size-8" /></div>
          )}
          <div role={qrState === "error" ? "alert" : "status"} className="text-muted-foreground text-center text-xs leading-relaxed">
            {qrState === "scanned" ? "微信已扫描，请在手机上确认；微博身份仍待验证" :
             qrState === "confirmed" ? "正在检查微博身份…" :
             qrState === "error" ? (qrError || "微信扫码流程出错，服务端尚未完成微博登录验证；请关闭后重试") :
             qrState === "expired" ? "二维码已过期，请关闭后重新获取" :
             qrState === "cancelled" ? (qrError || "本次扫码已取消，请关闭后重新获取") :
             "等待本人用微信扫码，尚未登录"}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => void cancelQr()}>取消</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function groupWeiboSources(sources: SourceInfo[]): { source: SourceInfo; discussions: SourceInfo[] }[] {
  const weibo = sources.filter((source) => source.platform === "weibo");
  const parents = new Set(weibo.filter((source) => !source.discussion_of).map((source) => source.id));
  return weibo.filter((source) => !source.discussion_of || !parents.has(source.discussion_of)).map((source) => ({
    source,
    discussions: source.discussion_of ? [] : weibo.filter((child) => child.discussion_of === source.id),
  }));
}

export default function Sources() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    // 独立任务由 worker 执行，活跃时勤轮询；空闲也刷新以发现自动定时任务。
    refetchInterval: (current) => {
      const sources = current.state.data?.sources ?? [];
      return sources.some(
        (s) => (s.import_status && importIsActive(s.import_status.state))
          || (s.platform === "weibo" && collectionIsActive(s.collection?.state ?? s.collector_status)),
      )
        ? 5_000
        : 30_000;
    },
  });

  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState<SourceInfo | null>(null);
  const [editing, setEditing] = useState<SourceInfo | null>(null);

  const invalidate = () => void client.invalidateQueries({ queryKey: ["sources"] });

  const create = useMutation({
    mutationFn: (body: { id: string; label: string; platform: string; mounted: boolean; role: SourceInfo["role"]; collector: WeiboCollectorConfig | null }) =>
      api.createSource(body),
    onSuccess: (res) => {
      setCreating(false);
      toast.success(`来源「${res.source.label}」已创建`);
      invalidate();
    },
    onError: (e: Error) => toast.error("创建失败", { description: e.message }),
  });

  const update = useMutation({
    mutationFn: (body: { id: string; label: string; role: SourceInfo["role"]; collector?: WeiboCollectorConfig | null }) =>
      api.updateSource(body.id, { label: body.label, role: body.role, ...(body.collector !== undefined ? { collector: body.collector } : {}) }),
    onSuccess: (res) => {
      setEditing(null);
      toast.success(`「${res.source.label}」已更新`);
      void client.invalidateQueries({ queryKey: ["source-collector", res.source.id] });
      invalidate();
    },
    onError: (e: Error) => {
      toast.error("保存失败", { description: e instanceof ApiError && e.status === 409
        ? `${e.message}。若任务仍在运行，请先停止本次；提交中需等待结束，再刷新重试。已绑定目标不能换 UID，请停用旧来源并新建。`
        : e.message });
      invalidate();
    },
  });

  const setMounted = useMutation({
    mutationFn: (body: { id: string; label: string; mounted: boolean }) =>
      api.setSourceMounted(body.id, body.mounted),
    onSuccess: (res, vars) => {
      toast.success(
        vars.mounted
          ? `「${res.source.label}」已启用，参与回答`
          : `「${res.source.label}」已停用，资料保留，不参与回答`,
      );
      void client.invalidateQueries({ queryKey: ["source-collector", res.source.id] });
      invalidate();
    },
    onError: (e: Error) => {
      toast.error("挂载状态未更改", { description: e instanceof ApiError && e.status === 409
        ? `${e.message}。请先停止本次采集；提交中需等待结束，再更改挂载。`
        : e.message });
      invalidate();
    },
  });

  // recovery_required：用空批次触发恢复（后端校验空批并执行 journal 恢复）
  const recover = useMutation({
    mutationFn: (id: string) => api.startImport(id, ""),
    onSuccess: (res, id) => {
      toast.success("已提交恢复请求，正在恢复…");
      // 找到来源并附上最新 import_status，让 ImportDialog 直接进轮询
      const src = data?.sources.find((s) => s.id === id);
      if (src) {
        setImporting({
          ...src,
          import_status: {
            state: res.state,
            job_id: res.job_id,
            records: res.records,
            new: res.new,
            updated: res.updated,
            unchanged: res.unchanged,
            chunks: res.chunks,
          },
        });
      }
      invalidate();
    },
    onError: (e: Error) => toast.error("恢复失败", { description: e.message }),
  });

  const data = query.data;
  const busy = create.isPending || update.isPending || setMounted.isPending || recover.isPending;
  const locked = busy || !data?.writable;

  const weiboSources = data?.sources.filter((s) => s.platform === "weibo") ?? [];
  const weiboGroups = groupWeiboSources(weiboSources);
  const otherDocumentSources = data?.sources.filter((s) => s.driver === "documents" && s.platform !== "weibo") ?? [];
  const wikiSources = data?.sources.filter((s) => s.driver === "mediawiki") ?? [];
  const takenIds = data?.sources.map((s) => s.id) ?? [];

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="资料来源"
        description="管理全部知识来源（Wiki 与微博）。停用来源后资料保留，不参与回答；恢复即生效。"
        actions={
          data?.writable ? (
            <Button size="sm" onClick={() => setCreating(true)} disabled={busy}>
              <Plus className="size-4" /> 新建微博来源
            </Button>
          ) : null
        }
      />

      {query.isPending ? (
        <div className="flex flex-col gap-2">
          {[1, 2].map((i) => <Skeleton key={i} className="h-32 rounded-xl" />)}
        </div>
      ) : query.isError || !data ? (
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      ) : (
        <>
          {!data.writable ? (
            <Banner tone="info" className="mb-0">
              没有配置覆写目录，这里只能查看，不能新建来源。
            </Banner>
          ) : null}

          {/* 微博来源区块 */}
          <div className="text-muted-foreground mt-1 px-1 text-xs font-medium">微博来源</div>
          {weiboSources.length === 0 ? (
            <Card>
              <EmptyState
                icon={Globe}
                title="还没有微博来源"
                hint={data.writable ? "点右上角「新建微博来源」添加第一个。" : "需要配置覆写目录才能添加。"}
              />
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {weiboGroups.map(({ source: s, discussions }) => (
                <section key={s.id} aria-label={`${s.label} 来源组`} className="flex flex-col gap-2">
                  <WeiboSourceCard source={s} locked={locked}
                    onToggleMounted={(mounted) => setMounted.mutate({ id: s.id, label: s.label, mounted })}
                    onOpenImport={() => setImporting(s)} onRecover={() => recover.mutate(s.id)} onEdit={() => setEditing(s)} />
                  {discussions.length ? <div className="ml-3 flex flex-col gap-2 border-l-2 pl-3">
                    <p className="text-muted-foreground text-xs">关联社区讨论 · 不是另一个官方账号来源</p>
                    {discussions.map((child) => <WeiboSourceCard key={child.id} source={child} locked={locked}
                      onToggleMounted={(mounted) => setMounted.mutate({ id: child.id, label: child.label, mounted })}
                      onOpenImport={() => setImporting(child)} onRecover={() => recover.mutate(child.id)} onEdit={() => setEditing(child)} />)}
                  </div> : null}
                </section>
              ))}
            </div>
          )}

          {otherDocumentSources.length > 0 ? <>
            <div className="text-muted-foreground mt-3 px-1 text-xs font-medium">其他文档来源</div>
            <div className="flex flex-col gap-2">
              {otherDocumentSources.map((s) => (
                <WeiboSourceCard key={s.id} source={s} locked={locked}
                  onToggleMounted={(mounted) => setMounted.mutate({ id: s.id, label: s.label, mounted })}
                  onOpenImport={() => setImporting(s)} onRecover={() => recover.mutate(s.id)} onEdit={() => setEditing(s)} />
              ))}
            </div>
          </> : null}

          {/* 微博共享访问会话与目标作者独立 */}
          <PlatformLoginCard hasWeibo={weiboSources.length > 0} />

          {/* Wiki 来源区块 */}
          <div className="text-muted-foreground mt-3 px-1 text-xs font-medium">Wiki 来源</div>
          {wikiSources.length === 0 ? (
            <Card>
              <EmptyState
                icon={BookOpen}
                title="没有挂载的 Wiki"
                hint={
                  <>
                    前往{" "}
                    <Link to="/wikis" className="underline underline-offset-2 hover:text-foreground">
                      Wiki 管理
                    </Link>{" "}
                    添加并挂载。
                  </>
                }
              />
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {wikiSources.map((s) => (
                <WikiSourceCard key={s.id} source={s} />
              ))}
            </div>
          )}

          {/* 刷新按钮 */}
          <div className="mt-2 flex justify-end px-1">
            <Button
              variant="ghost"
              size="xs"
              className="text-muted-foreground"
              onClick={invalidate}
              disabled={query.isFetching}
            >
              {query.isFetching ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <RefreshCw className="size-3.5" />
              )}
              刷新
            </Button>
          </div>
        </>
      )}

      <CreateSourceDialog
        open={creating}
        takenIds={takenIds}
        busy={create.isPending}
        onClose={() => setCreating(false)}
        onCreate={(id, label, role, collector) =>
          create.mutate({ id, label, platform: "weibo", mounted: true, role, collector })
        }
      />

      <ImportDialog
        source={importing}
        onClose={() => setImporting(null)}
        onDone={invalidate}
      />

      <EditSourceDialog
        source={editing}
        busy={update.isPending}
        onClose={() => setEditing(null)}
        onSave={(id, label, role, collector) => update.mutate({ id, label, role, collector })}
      />
    </div>
  );
}
