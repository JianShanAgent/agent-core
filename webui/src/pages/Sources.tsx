// 资料来源：列出全部来源（Wiki + 微博），微博支持创建、导入、停用/恢复。
// Wiki 来源展示简要信息并跳转 /wikis；微博来源完整管理闭环。

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
import type { ImportJob, ImportPreview, ImportState, ImportStatus, SourceInfo, WeiboSessionStatus } from "@/lib/types";
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
  onCreate: (id: string, label: string, role: SourceInfo["role"]) => void;
}) {
  const [id, setId] = useState("");
  const [label, setLabel] = useState("");
  const [role, setRole] = useState<SourceInfo["role"]>("unknown");

  useEffect(() => {
    if (open) { setId(""); setLabel(""); setRole("unknown"); }
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
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建微博来源</DialogTitle>
          <DialogDescription>
            新建后可上传标准资料 JSONL 进行导入。来源 ID 建好后不要改，它是知识库的命名空间。
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
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>取消</Button>
          <Button
            loading={busy}
            disabled={!id || !!idProblem || !label.trim()}
            onClick={() => onCreate(id, label.trim(), role)}
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

/** 采集器状态说明行（只显示文字，不提供交互按钮） */
function CollectorLine({ collectorStatus }: { collectorStatus: string }) {
  if (collectorStatus === "wiki_managed") return null; // Wiki 自己管，不在这里显示
  // not_configured 或其他文档来源状态
  return (
    <div className="text-muted-foreground flex items-center gap-1.5 text-xs">
      <span className="inline-block size-1.5 rounded-full bg-muted-foreground/50 shrink-0" />
      采集未配置，当前支持标准资料导入。
      {/* 预留：后续微博登录状态/二维码区块插入此处，参考 BWiki 登录行 */}
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
function WeiboSourceCard({
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
        <Badge tone="outline">微博</Badge>
        {!source.mounted ? <Badge tone="warning">已停用</Badge> : null}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="xs" onClick={onEdit} disabled={locked}>
            编辑
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
      <CollectorLine collectorStatus={source.collector_status} />

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
  onSave: (id: string, label: string, role: SourceInfo["role"]) => void;
}) {
  const [label, setLabel] = useState("");
  const [role, setRole] = useState<SourceInfo["role"]>("unknown");
  useEffect(() => {
    if (source) { setLabel(source.label); setRole(source.role); }
  }, [source]);
  return (
    <Dialog open={source !== null} onOpenChange={(o) => { if (!o && !busy) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>编辑来源 · {source?.id}</DialogTitle>
          <DialogDescription>修改显示名称和身份角色；ID 和平台不能更改。</DialogDescription>
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
              disabled={busy}
              className="w-full"
              options={[
                { value: "unknown", label: "身份未知" },
                { value: "official", label: "官方" },
                { value: "community", label: "社区" },
              ]}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>取消</Button>
          <Button
            loading={busy}
            disabled={!label.trim()}
            onClick={() => source && onSave(source.id, label.trim(), role)}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- 微博平台共享采集会话登录卡片 ----
// 注意：此处登录账户是采集账号，与帖子作者（source.role）无关。
// 默认走官方 HTTP 微信扫码，不依赖服务器浏览器；本地保存不等于远端已验证。

function PlatformLoginCard({ hasWeibo }: { hasWeibo: boolean }) {
  const [qrOpen, setQrOpen] = useState(false);
  const [qrImage, setQrImage] = useState<string | null>(null);
  const [qrState, setQrState] = useState("");
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
          toast.error("登录会话已变化，请关闭弹窗后刷新页面");
          return;
        }
        setQrState(res.state);
        if (res.state === "confirmed") {
          setQrOpen(false);
          setQrImage(null);
          setQrLoginId(null);
          loginIdRef.current = null;
          void refreshStatus();
          if (res.verified && res.valid === true) {
            toast.success("微博登录已验证；资料采集仍未配置");
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
    ? "微博登录已验证；资料采集仍未配置"
    : !configured
      ? "未配置采集会话"
      : locallyUsable
        ? "会话已保存，采集可用性待验证"
        : "本地会话已失效或无法读取，请重新微信扫码";

  return (
    <Card className="flex flex-col gap-0 overflow-hidden p-0">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-4 py-3">
        <div>
          <div className="text-sm font-semibold">微博采集账号（共享）</div>
          <div className="text-muted-foreground mt-0.5 max-w-2xl text-xs leading-relaxed">
            所有微博来源共用同一个采集登录身份，与各来源帖子的作者无关。
            当前采集尚未配置，本登录仅备用；不提供抓取按钮。
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
          <Hint text={loginUnavailable ? status?.message || "微信扫码登录组件暂不可用" : "微信扫码，通过官方 HTTP 接口，无需服务器浏览器"}>
            <span>
              <Button
                size="xs"
                variant={configured ? "outline" : "default"}
                loading={qrBusy}
                disabled={qrBusy || loginUnavailable || statusQuery.isPending}
                onClick={() => void startQr()}
              >
                <QrCode className="size-3.5" />
                {configured ? "重新微信扫码" : "微信扫码登录"}
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
              请本人用微信扫描官方二维码并确认，无需服务器浏览器。此登录身份与帖子作者无关；资料采集仍未配置。
            </DialogDescription>
          </DialogHeader>
          {qrImage ? (
            <img src={qrImage} alt="微信扫码登录微博的官方二维码" className="mx-auto size-56" />
          ) : (
            <div className="flex justify-center py-8"><Spinner className="size-8" /></div>
          )}
          <div className="text-muted-foreground text-center text-xs">
            {qrState === "scanned" ? "微信已扫描，请在手机上确认；微博身份仍待验证" :
             qrState === "confirmed" ? "正在检查微博身份…" :
             ["expired", "error", "cancelled"].includes(qrState) ? "本次登录已停止，请关闭后重试" :
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

export default function Sources() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    // 有进行中导入时勤轮询
    refetchInterval: (current) => {
      const sources = current.state.data?.sources ?? [];
      return sources.some(
        (s) => s.import_status && importIsActive(s.import_status.state),
      )
        ? 5_000
        : false;
    },
  });

  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState<SourceInfo | null>(null);
  const [editing, setEditing] = useState<SourceInfo | null>(null);

  const invalidate = () => void client.invalidateQueries({ queryKey: ["sources"] });

  const create = useMutation({
    mutationFn: (body: { id: string; label: string; platform: string; mounted: boolean; role: SourceInfo["role"] }) =>
      api.createSource(body),
    onSuccess: (res) => {
      setCreating(false);
      toast.success(`来源「${res.source.label}」已创建`);
      invalidate();
    },
    onError: (e: Error) => toast.error("创建失败", { description: e.message }),
  });

  const update = useMutation({
    mutationFn: (body: { id: string; label: string; role: SourceInfo["role"] }) =>
      api.updateSource(body.id, { label: body.label, role: body.role }),
    onSuccess: (res) => {
      setEditing(null);
      toast.success(`「${res.source.label}」已更新`);
      invalidate();
    },
    onError: (e: Error) => toast.error("保存失败", { description: e.message }),
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
      invalidate();
    },
    onError: (e: Error) => toast.error("操作失败", { description: e.message }),
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

  const weiboSources = data?.sources.filter((s) => s.driver === "documents") ?? [];
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
              {weiboSources.map((s) => (
                <WeiboSourceCard
                  key={s.id}
                  source={s}
                  locked={locked}
                  onToggleMounted={(mounted) =>
                    setMounted.mutate({ id: s.id, label: s.label, mounted })
                  }
                  onOpenImport={() => setImporting(s)}
                  onRecover={() => recover.mutate(s.id)}
                  onEdit={() => setEditing(s)}
                />
              ))}
            </div>
          )}

          {/* 微博平台采集账号（所有微博来源共用，与帖子作者无关） */}
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
        onCreate={(id, label, role) =>
          create.mutate({ id, label, platform: "weibo", mounted: true, role })
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
        onSave={(id, label, role) => update.mutate({ id, label, role })}
      />
    </div>
  );
}
