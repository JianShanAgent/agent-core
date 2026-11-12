// Wiki：机器人挂着哪些 wiki；从站点目录里按平台挑一个做好了的站，点一下就挂上。
// 部署的配置文件里写的 wiki 在这里只能看；在这里加的可以移除。加上以后 /查 马上能用，
// 知识库不跟着建（要把整个 wiki 取一遍、算向量）：另点「建知识库」，由自动更新在后台做。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleArrowUp,
  Database,
  Download,
  ExternalLink,
  Globe,
  KeyRound,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Banner, useSystemActions } from "@/components/layout/StateBanners";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/form";
import { Hint, Skeleton } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { formatAgo } from "@/lib/format";
import { useRefreshConfig } from "@/lib/hooks";
import type { CatalogSite, SitePluginInfo, WikiInfo, WikisInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

const SOURCES: Record<SitePluginInfo["source"], string> = {
  environment: "随部署带着",
  downloaded: "在这里下载的",
  local: "实例的 sites/ 目录",
  path: "按包路径加载",
};

const WIKI_ID = /^[a-z][a-z0-9_]{0,31}$/;

function Mono({ children }: { children: ReactNode }) {
  return <code className="font-mono text-xs">{children}</code>;
}

function pluginText(site: SitePluginInfo): string {
  return `${site.id}${site.version ? ` v${site.version}` : ""}（${SOURCES[site.source]}）`;
}

/** 知识库那一行：现在是什么情况，能做什么。 */
function KbLine({
  wiki,
  info,
  locked,
  onBuild,
  onCancel,
}: {
  wiki: WikiInfo;
  info: WikisInfo;
  locked: boolean;
  onBuild: () => void;
  onCancel: () => void;
}) {
  const { kb } = wiki;
  if (kb.state === "ready") {
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Badge tone="success">知识库已建好</Badge>
        <span className="text-muted-foreground text-xs">
          自动更新照常做增量；内容和状态在「知识库」页里看。
        </span>
      </div>
    );
  }
  if (kb.state === "requested") {
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Badge tone="info">等自动更新接手</Badge>
        <span className="text-muted-foreground text-xs">
          {formatAgo(kb.requested_at)}点的「建知识库」
          {info.kb.sync_alive ? "，一分钟内开始。" : "。自动更新服务（kbsync）没在运行，它起来以后才会开始。"}
        </span>
        <Button variant="ghost" size="xs" disabled={locked} onClick={onCancel}>
          撤回
        </Button>
      </div>
    );
  }
  if (kb.state === "building") {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Badge tone="info">正在建知识库</Badge>
          <span className="text-muted-foreground text-xs">
            {formatAgo(kb.started_at)}开始的：全量取数、切片、算向量、导入。进度在自动更新服务的日志里。
          </span>
        </div>
        {kb.problem ? <div className="text-warning text-xs break-words">{kb.problem}</div> : null}
      </div>
    );
  }
  const blocked = !info.kb.plugin
    ? "知识库插件（knowledge）没有启用"
    : !info.kb.embedding
      ? "没有配置嵌入接口（core.embedding）"
      : "";
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <Badge tone="muted">还没有知识库</Badge>
      <span className="text-muted-foreground text-xs">
        {blocked || "/查 已经能用；要让它的内容进对话和查证，得先建知识库。"}
      </span>
      <Hint text={blocked}>
        <span>
          <Button variant="outline" size="xs" disabled={locked || !!blocked} onClick={onBuild}>
            <Database className="size-3.5" /> 建知识库
          </Button>
        </span>
      </Hint>
    </div>
  );
}

function WikiCard({
  wiki,
  info,
  platformName,
  locked,
  onRemove,
  onUpdate,
  onBuild,
  onCancel,
}: {
  wiki: WikiInfo;
  info: WikisInfo;
  platformName: string;
  locked: boolean;
  onRemove: () => void;
  onUpdate: () => void;
  onBuild: () => void;
  onCancel: () => void;
}) {
  return (
    <Card className="flex flex-col gap-2.5 p-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="text-sm font-semibold">{wiki.label}</span>
        <Mono>{wiki.id}</Mono>
        {wiki.primary ? (
          <Hint text="排第一的 wiki：对话、查证、人设用的是它的站点插件的补充">
            <Badge tone="brand">主 wiki</Badge>
          </Hint>
        ) : null}
        <Badge tone="outline">{platformName}</Badge>
        <Badge tone={wiki.added ? "default" : "muted"}>
          {wiki.added ? "在这里加的" : "部署的配置文件里写的"}
        </Badge>
        {wiki.running ? null : (
          <Hint text="盘上的配置里有它，正在运行的这一套里还没有：重新加载或者重启以后生效">
            <Badge tone="warning">还没生效</Badge>
          </Hint>
        )}
        <div className="ml-auto flex items-center gap-1">
          {wiki.site?.source === "downloaded" ? (
            <Hint text="看这个站点插件的仓库有没有新的发布，有就换上并重启机器人">
              <Button variant="ghost" size="xs" disabled={locked} onClick={onUpdate}>
                <CircleArrowUp className="size-3.5" /> 更新插件
              </Button>
            </Hint>
          ) : null}
          {wiki.added ? (
            <Button
              variant="ghost"
              size="xs"
              className="text-destructive"
              disabled={locked}
              onClick={onRemove}
            >
              <Trash2 className="size-3.5" /> 移除
            </Button>
          ) : null}
        </div>
      </div>
      <div className="text-muted-foreground flex flex-col gap-1 text-xs">
        <div className="break-all">
          接口 <Mono>{wiki.api_url}</Mono>
        </div>
        <div>
          站点插件{" "}
          {wiki.site ? (
            <>
              <span className="text-foreground/80">{wiki.site.name}</span>{" "}
              <Mono>{pluginText(wiki.site)}</Mono>
            </>
          ) : (
            "没有用（只用平台自带的那一份）"
          )}
        </div>
        {wiki.auth_header ? (
          <div className="flex flex-wrap items-center gap-x-1.5">
            <KeyRound className="size-3.5" />
            {wiki.auth_key_set ? (
              <span>
                请求带着放行请求头 <Mono>{wiki.auth_header}</Mono>，密钥已经填了。
              </span>
            ) : (
              <span className="text-warning">
                这个站要带放行请求头 <Mono>{wiki.auth_header}</Mono> 才不会被拦：在{" "}
                <Mono>secrets.yaml</Mono> 里填 <Mono>wiki.{wiki.id}.authkey</Mono>，然后重启机器人。
              </span>
            )}
          </div>
        ) : null}
      </div>
      <KbLine wiki={wiki} info={info} locked={locked} onBuild={onBuild} onCancel={onCancel} />
    </Card>
  );
}

function CatalogCard({
  site,
  platformName,
  locked,
  onAdd,
}: {
  site: CatalogSite;
  platformName: string;
  locked: boolean;
  onAdd: () => void;
}) {
  return (
    <Card className="flex flex-col gap-2 p-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="text-sm font-semibold">{site.name}</span>
        <Mono>{site.id}</Mono>
        <Badge tone="outline">{platformName}</Badge>
        {site.wiki ? (
          <Badge tone="success">已挂上（{site.wiki}）</Badge>
        ) : site.installed ? (
          <Badge tone="muted">插件{SOURCES[site.installed.source]}</Badge>
        ) : (
          <Badge tone="muted">
            <Download /> 添加时下载
          </Badge>
        )}
        <div className="ml-auto">
          {site.wiki ? null : (
            <Hint text={site.unavailable}>
              <span>
                <Button size="xs" disabled={locked || !!site.unavailable} onClick={onAdd}>
                  <Plus className="size-3.5" /> 添加
                </Button>
              </span>
            </Hint>
          )}
        </div>
      </div>
      {site.description ? (
        <div className="text-foreground/80 text-[13px] leading-relaxed">{site.description}</div>
      ) : null}
      <div className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="break-all">
          接口 <Mono>{site.api_url}</Mono>
        </span>
        <a
          className="hover:text-foreground inline-flex items-center gap-1 underline-offset-2 hover:underline"
          href={`https://github.com/${site.repo}`}
          target="_blank"
          rel="noreferrer"
        >
          {site.repo} <ExternalLink className="size-3" />
        </a>
      </div>
      {site.unavailable ? <div className="text-warning text-xs">{site.unavailable}</div> : null}
    </Card>
  );
}

/** 添加之前：给它起名字。地址、用哪个站点插件照目录里的，不能改。 */
function AddDialog({
  site,
  taken,
  busy,
  onClose,
  onAdd,
}: {
  site: CatalogSite | null;
  taken: string[];
  busy: boolean;
  onClose: () => void;
  onAdd: (id: string, label: string) => void;
}) {
  const [id, setId] = useState("");
  const [label, setLabel] = useState("");
  useEffect(() => {
    if (site) {
      setId(site.wiki_id);
      setLabel(site.label);
    }
  }, [site]);
  const problem = !WIKI_ID.test(id)
    ? "小写字母开头，只用小写字母、数字、下划线"
    : taken.includes(id)
      ? "已经有一个叫这个名字的 wiki 了"
      : "";
  return (
    <Dialog open={site !== null} onOpenChange={(open) => (open || busy ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>添加「{site?.name}」</DialogTitle>
          <DialogDescription>
            {site?.installed
              ? "站点插件已经在这台机器上，加上就能用。"
              : `这台机器上还没有它的站点插件：会从 ${site?.repo} 最新的发布下载，核对校验和以后装到数据目录里。`}
            加上以后 /查 马上能用，不用重启；知识库要另外点「建知识库」。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="wiki-id">这个 wiki 在这个部署里的名字</Label>
            <Input
              id="wiki-id"
              className="font-mono"
              value={id}
              aria-invalid={!!problem}
              disabled={busy}
              onChange={(event) => setId(event.target.value.trim())}
            />
            <div className={cn("text-xs", problem ? "text-destructive" : "text-muted-foreground")}>
              {problem || "知识库的目录名、放行密钥的键（wiki.<名字>.authkey）。建了知识库以后不要改。"}
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="wiki-label">回答里标出处时怎么称呼它</Label>
            <Input
              id="wiki-label"
              value={label}
              maxLength={40}
              disabled={busy}
              onChange={(event) => setLabel(event.target.value)}
            />
            <div className="text-muted-foreground text-xs">
              挂了不止一个 wiki 时才会标出处；不写就用上面的名字。
            </div>
          </div>
          {site?.auth_header ? (
            <div className="border-warning/40 bg-warning/8 rounded-md border px-3 py-2 text-xs leading-relaxed">
              这个站的请求要带放行请求头 <Mono>{site.auth_header}</Mono>。加上以后在{" "}
              <Mono>secrets.yaml</Mono> 里填 <Mono>wiki.{id || "<名字>"}.authkey</Mono>{" "}
              并重启机器人，不然请求会被拦。
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button loading={busy} disabled={!!problem} onClick={() => onAdd(id, label.trim())}>
            {site?.installed ? "添加" : "下载并添加"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type Ask =
  | { kind: "remove"; wiki: WikiInfo }
  | { kind: "build"; wiki: WikiInfo }
  | { kind: "update"; wiki: WikiInfo };

export default function Wikis() {
  const refresh = useRefreshConfig();
  const client = useQueryClient();
  const actions = useSystemActions();
  const query = useQuery({
    queryKey: ["wikis"],
    queryFn: api.wikis,
    // 有知识库在等、在建的时候勤看着点
    refetchInterval: (current) =>
      current.state.data?.wikis.some((wiki) => ["requested", "building"].includes(wiki.kb.state))
        ? 15_000
        : false,
  });
  const catalog = useQuery({
    queryKey: ["wikis", "catalog"],
    queryFn: () => api.wikiCatalog(),
    retry: false,
  });
  const [platform, setPlatform] = useState("");
  const [adding, setAdding] = useState<CatalogSite | null>(null);
  const [asking, setAsking] = useState<Ask | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const done = () => {
    refresh();
    void client.invalidateQueries({ queryKey: ["wikis"] });
  };
  const failed = (title: string) => (error: Error) => {
    toast.error(title, { description: error.message });
    done();
  };

  const add = useMutation({
    mutationFn: (input: { site: string; id: string; label: string }) =>
      api.addWiki(input.site, input.id, input.label),
    onSuccess: (result) => {
      setAdding(null);
      const wiki = result.added;
      toast.success(`已挂上「${wiki.label}」，/查 现在就能用`, {
        description:
          (result.downloaded ? `站点插件 v${result.downloaded} 已下载。` : "") +
          (wiki.auth_header && !wiki.auth_key_set
            ? `别忘了在 secrets.yaml 里填 wiki.${wiki.id}.authkey 并重启。`
            : "知识库要另外点「建知识库」。"),
      });
      done();
    },
    onError: failed("没加上，机器人仍按原来的配置运行"),
  });
  const remove = useMutation({
    mutationFn: (wiki: WikiInfo) => api.removeWiki(wiki.id),
    onSuccess: (result) => {
      toast.success(`已移除「${result.removed}」`, {
        description:
          (result.site_removed ? "下载的站点插件一起删掉了。" : "") +
          "知识库里它的内容会在自动更新的下一轮清掉。",
      });
      done();
    },
    onError: failed("没移除成"),
  });
  const build = useMutation({
    mutationFn: (wiki: WikiInfo) => api.buildWikiKb(wiki.id),
    onSuccess: (result) => {
      toast.success(
        result.sync_alive
          ? "已经交给自动更新，一分钟内开始建"
          : "请求放好了；自动更新服务（kbsync）没在运行，它起来以后才会开始",
      );
      done();
    },
    onError: failed("没法建知识库"),
  });
  const cancel = useMutation({
    mutationFn: (wiki: WikiInfo) => api.cancelWikiKb(wiki.id),
    onSuccess: () => {
      toast.success("已撤回");
      done();
    },
    onError: failed("没撤回成"),
  });
  const update = useMutation({
    mutationFn: (wiki: WikiInfo) => api.updateSitePlugin(wiki.site?.id ?? ""),
    onSuccess: (result) => {
      if (!result.updated) {
        toast.success(`已经是最新的发布了（v${result.version}）`);
        return;
      }
      // 后端已经在重启了：等它起来
      void actions.restartWith(async () => {}, `站点插件已更新到 v${result.version}，机器人已重启`);
    },
    onError: failed("站点插件没更新成"),
  });

  const data = query.data;
  const busy =
    add.isPending || remove.isPending || update.isPending || actions.restarting || build.isPending;
  const locked = busy || !data?.state.writable;
  const platformName = (id: string) => data?.platforms.find((item) => item.id === id)?.name ?? id;
  const sites = catalog.data?.sites ?? [];
  const shown = platform ? sites.filter((site) => site.platform === platform) : sites;
  const counts = new Map<string, number>();
  for (const site of sites) counts.set(site.platform, (counts.get(site.platform) ?? 0) + 1);

  const reloadCatalog = async () => {
    setRefreshing(true);
    try {
      client.setQueryData(["wikis", "catalog"], await api.wikiCatalog(true));
    } catch (error) {
      toast.error("站点目录没读成", { description: (error as Error).message });
    } finally {
      setRefreshing(false);
    }
  };

  const confirm = () => {
    if (!asking) return;
    const { kind, wiki } = asking;
    setAsking(null);
    if (kind === "remove") remove.mutate(wiki);
    else if (kind === "build") build.mutate(wiki);
    else update.mutate(wiki);
  };

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="Wiki"
        description="机器人挂着哪些 wiki。做好了的站点可以从站点目录里挑，点一下就挂上，不用重启。"
      />

      {query.isPending ? (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 2 }, (_, index) => (
            <Skeleton key={index} className="h-28 rounded-xl" />
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
              没有配置覆写目录（环境变量 <Mono>JIANSHANAGENT_CONFIG_OVERRIDES_DIR</Mono>{" "}
              没设），这里只能看，不能添加。
            </Banner>
          )}
          {data.skipped ? (
            <Banner tone="warning" className="mb-0">
              {data.skipped}
            </Banner>
          ) : null}

          <div className="text-muted-foreground mt-1 px-1 text-xs font-medium">挂着的 wiki</div>
          {data.wikis.length === 0 ? (
            <Card>
              <EmptyState
                icon={Globe}
                title="一个 wiki 都没挂"
                hint="从下面的站点目录里挑一个添加；自己的站写在部署的 config/core.wiki.yaml 里。"
              />
            </Card>
          ) : (
            <div className="flex flex-col gap-2">
              {data.wikis.map((wiki) => (
                <WikiCard
                  key={wiki.id}
                  wiki={wiki}
                  info={data}
                  platformName={platformName(wiki.platform)}
                  locked={locked}
                  onRemove={() => setAsking({ kind: "remove", wiki })}
                  onUpdate={() => setAsking({ kind: "update", wiki })}
                  onBuild={() => setAsking({ kind: "build", wiki })}
                  onCancel={() => cancel.mutate(wiki)}
                />
              ))}
            </div>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 px-1">
            <div className="text-muted-foreground text-xs font-medium">站点目录</div>
            <div className="flex flex-wrap items-center gap-1">
              {[{ id: "", name: "全部" }, ...data.platforms.filter((p) => counts.has(p.id))].map(
                (item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setPlatform(item.id)}
                    className={cn(
                      "cursor-pointer rounded-md border px-2 py-0.5 text-xs transition-colors",
                      platform === item.id
                        ? "bg-secondary text-foreground border-transparent font-medium"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {item.name}
                    <span className="ml-1 opacity-70">
                      {item.id ? counts.get(item.id) : sites.length}
                    </span>
                  </button>
                ),
              )}
            </div>
            <Button
              variant="ghost"
              size="xs"
              className="text-muted-foreground ml-auto"
              loading={refreshing}
              onClick={() => void reloadCatalog()}
            >
              {refreshing ? null : <RefreshCw className="size-3.5" />} 刷新
            </Button>
          </div>

          {!data.catalog.enabled ? (
            <Card>
              <EmptyState
                icon={Globe}
                title="站点目录关着"
                hint={data.catalog.error || "core.catalog 的 enabled 是 false。"}
              />
            </Card>
          ) : catalog.isPending ? (
            <Skeleton className="h-24 rounded-xl" />
          ) : catalog.isError ? (
            <Card>
              <ErrorState error={catalog.error} onRetry={() => void catalog.refetch()} />
            </Card>
          ) : (
            <>
              {catalog.data.error ? (
                <Banner tone="warning" className="mb-0">
                  站点目录（{catalog.data.repo || data.catalog.repo}）这次没读成
                  {sites.length ? "，下面是上一次读到的" : ""}：{catalog.data.error}
                </Banner>
              ) : null}
              {shown.length === 0 && !catalog.data.error ? (
                <Card>
                  <EmptyState icon={Globe} title="目录里还没有站点" />
                </Card>
              ) : (
                <div className="flex flex-col gap-2">
                  {shown.map((site) => (
                    <CatalogCard
                      key={site.id}
                      site={site}
                      platformName={platformName(site.platform)}
                      locked={locked}
                      onAdd={() => setAdding(site)}
                    />
                  ))}
                </div>
              )}
              {catalog.data.problems?.length ? (
                <div className="text-muted-foreground px-1 text-xs">
                  目录里有几项没用上：{catalog.data.problems.join("；")}
                </div>
              ) : null}
            </>
          )}

          <p className="text-muted-foreground mt-1 max-w-4xl px-1 text-xs leading-relaxed">
            站点目录是 <Mono>{data.catalog.repo}</Mono>{" "}
            仓库里的一份名单，只列同一个组织下的站点插件；wiki
            的地址、用哪个站点插件都照目录里写的，这里只能给它起名字。各个 wiki
            平台（灰机、BWiki）多出来的东西核心自己带着，站点插件只带某一个站特有的。在这里加的 wiki
            存在数据目录里，发布新版本不会丢；部署的配置文件里写的 wiki 要改那个文件再发布。
          </p>
        </>
      )}

      <AddDialog
        site={adding}
        taken={data?.wikis.map((wiki) => wiki.id) ?? []}
        busy={add.isPending}
        onClose={() => setAdding(null)}
        onAdd={(id, label) => (adding ? add.mutate({ site: adding.id, id, label }) : undefined)}
      />
      <ConfirmDialog
        open={asking !== null}
        onOpenChange={(open) => {
          if (!open) setAsking(null);
        }}
        title={
          asking?.kind === "remove"
            ? `移除「${asking.wiki.label}」？`
            : asking?.kind === "build"
              ? `给「${asking.wiki.label}」建知识库？`
              : `更新「${asking?.wiki.site?.name ?? ""}」的站点插件？`
        }
        description={
          asking?.kind === "remove" ? (
            <>
              /查 不再查它，对话和查证也不再用它的内容；马上生效，不用重启。
              {asking.wiki.site?.source === "downloaded" ? "下载的站点插件一起删掉。" : ""}
              知识库里它的内容会在自动更新的下一轮清掉，数据目录里它的文件留着。
            </>
          ) : asking?.kind === "build" ? (
            <>
              会把整个 wiki 取一遍、切片、算向量、导入数据库，由自动更新服务（kbsync）在后台做，
              机器人不用停。大的 wiki 要几十分钟到几个小时，这期间别的 wiki
              的自动更新排在它后面；算向量按嵌入接口的用量计费。
              {data?.kb.sync_alive ? "" : " 现在自动更新服务没在运行：请求会留着，它起来以后才开始。"}
            </>
          ) : (
            <>
              看 <Mono>{asking?.wiki.site?.id}</Mono>{" "}
              的仓库有没有比现在新的发布；有就下载、核对校验和、换上，然后重启机器人（大约十几秒）。
            </>
          )
        }
        confirmText={
          asking?.kind === "remove" ? "移除" : asking?.kind === "build" ? "开始建" : "检查并更新"
        }
        destructive={asking?.kind === "remove"}
        onConfirm={confirm}
      />
    </div>
  );
}
