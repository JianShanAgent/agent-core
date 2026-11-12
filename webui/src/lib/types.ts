// 后端接口返回的数据的形状（对应 src/jianshanagent/admin/routes_*.py）。

export interface ConfigState {
  /** 有没有配置覆写目录：没有的话网页上改不了配置 */
  writable: boolean;
  overrides: string[];
  override_errors: Record<string, string>;
  /** 覆写让机器人起不来、启动时被整个忽略的原因；没有这回事是空字符串 */
  overrides_ignored: string;
  /** 盘上改了、要重启才生效的配置文件 */
  pending_restart: string[];
  /** 盘上改了、重新加载就生效的配置文件 */
  pending_reload: string[];
}

/** 数据库备份的情况（备份容器每次备份完记一行，后台读出来） */
export interface BackupState {
  /** 最近一次备份；一次都还没有记过是 null */
  last: {
    finished_at: string;
    ok: boolean;
    file: string;
    bytes: number;
    /** 有没有另存一份到第二个存放处 */
    copied: boolean;
    /** 哪里出了问题；一切正常时是空的 */
    detail: string;
  } | null;
  last_ok_at: string | null;
  /** 从什么时候开始有备份记录 */
  since: string | null;
  /** 超过这么多小时没有成功的备份就提醒；0 = 不提醒 */
  warn_hours: number;
  overdue: boolean;
}

export interface Status {
  version: string;
  release: string | null;
  /** 主仓库有没有比现在新的版本（细节在 /api/update） */
  update: { available: boolean; latest: string | null };
  now: number;
  started_at: number;
  uptime_seconds: number;
  loaded_at: number;
  generation: number;
  dry_run: boolean;
  timezone: string;
  gateway: {
    connected: boolean;
    connected_at: number | null;
    disconnects: number;
    events: number;
    last_event_at: number | null;
  } | null;
  database: boolean;
  llm: boolean;
  plugins: string[];
  features: Record<string, boolean>;
  active: number;
  /** 没有数据库时是 null */
  backup: BackupState | null;
  config: ConfigState;
}

/** 一条处理记录在列表里的样子 */
export interface TraceRow {
  id: string;
  started_at: string;
  seconds: number;
  finished?: boolean;
  channel: string;
  scope: string;
  chat_id: string;
  user_id: string;
  user_name: string | null;
  session_key: string;
  message_id: string;
  question: string;
  images: number;
  route: string;
  command: string;
  status: string;
  outcome: string;
  reply: string;
  error: string;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  summary: TraceSummary;
  /** 还在处理的记录：现在走到哪一步了 */
  stage?: string;
  /** 在后台里给这个会话（群）、这个人起的备注名；没起就是空的 */
  chat_label?: string;
  user_label?: string;
}

export interface TraceSummary {
  research?: {
    topic?: string;
    query?: string;
    stages?: string[];
    evidence?: number;
    rejections?: number;
    fixed?: boolean;
    problems?: string[];
    cited?: { title: string; url: string }[];
    seconds?: number;
  };
}

export interface EvidenceItem {
  number: number;
  origin: string;
  kind: string;
  title: string;
  url: string;
  date: string;
  key: string;
  chars: number;
  /** 只是所在页面的一部分（页面太长，没有整页给模型） */
  excerpt?: boolean;
  preview: string;
}

export interface TraceStep {
  id: number;
  kind: string;
  title: string;
  at: number;
  /** null = 一闪而过的事（发了一句进度） */
  seconds: number | null;
  ok: boolean;
  data: Record<string, unknown>;
}

export interface LlmCall {
  id: number;
  trace_id: string | null;
  step_id: number | null;
  started_at: string;
  seconds: number;
  purpose: string;
  profile: string;
  model: string;
  thinking: boolean;
  ok: boolean;
  finish_reason: string;
  error: string;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  tool_calls: number;
  has_detail: boolean;
}

export interface ChatMessagePart {
  role: string;
  content: string | null;
  tool_calls?: { id?: string; function?: { name?: string; arguments?: string } }[];
  tool_call_id?: string;
  reasoning_content?: string;
}

export interface LlmCallDetail extends LlmCall {
  request: {
    messages: ChatMessagePart[];
    tools: string[];
    json_mode: boolean;
  } | null;
  response: {
    content: string;
    reasoning: string;
    tool_calls: { id?: string; function?: { name?: string; arguments?: string } }[];
  } | null;
}

export interface TraceDetail extends TraceRow {
  finished: boolean;
  steps: TraceStep[];
  calls: LlmCall[];
}

export interface SeriesPoint {
  t: string;
  messages: number;
  research: number;
  errors: number;
  median_seconds: number;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface UsageRow {
  calls: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  avg_seconds: number | null;
  p90_seconds: number | null;
}

export interface OverviewStats {
  totals: {
    messages: number;
    replied: number;
    errors: number;
    research: number;
    answered: number;
    not_found: number;
    failed: number;
    commands: number;
    chitchat: number;
    limited: number;
    users: number;
    groups: number;
    median_seconds: number;
    p90_seconds: number;
    research_median_seconds: number;
    research_p90_seconds: number;
    llm_calls: number;
    llm_failed: number;
    prompt_tokens: number;
    completion_tokens: number;
    reasoning_tokens: number;
    cached_tokens: number;
  };
  /** 查证用时分布：按 duration_edges 分档，比它多一格 */
  durations: number[];
  series: SeriesPoint[];
  routes: { route: string; count: number }[];
  topics: { topic: string; count: number }[];
  profiles: (UsageRow & { profile: string; model: string; thinking: boolean })[];
  purposes: (UsageRow & { purpose: string })[];
}

export interface Overview {
  status: Status;
  range: { hours: number; daily: boolean; channel: string | null };
  duration_edges: number[];
  active: TraceRow[];
  /** 没有数据库时是 null */
  stats: OverviewStats | null;
  recent: TraceRow[];
}

export interface SystemInfo {
  status: Status;
  environment: {
    python: string;
    platform: string;
    pid: number;
    in_container: boolean;
    config_dir: string;
    overrides_dir: string | null;
    static_ready: boolean;
    token_source: string;
    logs_url: string;
  };
  secrets: { key: string; set: boolean }[];
  tables: { name: string; rows: number; bytes: number }[];
}

/** 核心的更新：正在用哪个版本、主仓库最新是哪个、能不能在这里点更新 */
export interface UpdateInfo {
  enabled: boolean;
  repo: string;
  /** 正在用的版本 */
  current: string;
  /** 环境里装的版本；用着更新包时和 current 不一样 */
  installed: string;
  overlay: boolean;
  trial: boolean;
  /** overlay = 可以在程序里更新；managed = 按发布流程部署的；source = 从源码运行；none = 没有地方放更新包 */
  mode: "overlay" | "managed" | "source" | "none";
  mode_reason: string;
  latest: {
    version: string;
    tag: string;
    name: string;
    notes: string;
    published_at: string;
    url: string;
    size: number;
    has_package: boolean;
  } | null;
  available: boolean;
  checked_at: number | null;
  error: string;
  can_apply: boolean;
  /** 有新版本、但不能在这里点更新的原因 */
  blocked: string;
  applying: boolean;
  last_failure: { version: string; error: string; at: number } | null;
}

export interface LogEntry {
  seq: number;
  time: string;
  level: string;
  event: string;
  fields: Record<string, unknown>;
}

// ---- 配置 ----

export type FieldKind = "group" | "bool" | "int" | "float" | "str" | "text" | "list" | "json";

export interface SchemaField {
  key: string;
  path: string[];
  kind: FieldKind;
  description: string;
  /** 默认值里没有、覆写里加出来的项 */
  added: boolean;
  /** 不能在网页上改（接口地址、用哪个密钥……）：只显示，要改就改仓库里的配置文件再发布 */
  locked?: boolean;
  /** kind 是 list 时：里面的项是 str 还是 number */
  items?: string;
  children?: SchemaField[];
}

/** 一份配置里哪些项不能在网页上改。名单是后端给的（保存时拦的也是它），页面不自己记一份。 */
export interface LockedRules {
  /** 叫这些名字的项，不管在哪一层 */
  keys: string[];
  /** 另外按路径锁住的项 */
  paths: string[][];
}

export interface ConfigFileSummary {
  name: string;
  group: string;
  summary: string;
  /** reload = 保存后马上生效；restart = 要重启机器人才生效 */
  mode: "reload" | "restart";
  /** 改了几项 */
  overridden: number;
  error: string | null;
}

export type ConfigValues = Record<string, unknown>;

export interface ConfigFile extends ConfigFileSummary {
  description: string;
  schema: SchemaField[];
  /** 哪些项不能在网页上改：schema 里每一项各带一个 locked，这次编辑里新加的项照这个判断 */
  locked: LockedRules;
  base: ConfigValues;
  effective: ConfigValues;
  override: ConfigValues;
  /** 默认值文件的原文（带注释） */
  source: string;
  override_text: string;
  state: ConfigState;
}

export interface SaveResult {
  saved: string[];
  needs_restart: string[];
  status: Status;
}

export interface ModelProfile {
  name: string;
  model: string;
  thinking: boolean;
  max_completion_tokens: number | null;
  temperature: number | null;
  description: string;
  added: boolean;
  usage: {
    calls: number;
    failed: number;
    avg_seconds: number;
    p90_seconds: number;
    tokens: number;
    last_at: string;
  } | null;
}

export interface ModelRole {
  id: string;
  label: string;
  hint: string;
  profile: string;
  /** 这个环节用哪一档写在哪份配置的哪一项；null = 写死在代码里，改不了 */
  file: string | null;
  path: string[];
}

export interface ModelsInfo {
  enabled: boolean;
  running: boolean;
  base_url: string;
  /** 接口地址不能在网页上改 */
  base_url_locked: boolean;
  default_profile: string;
  timeout_seconds: number;
  web_search: boolean;
  profiles: ModelProfile[];
  roles: ModelRole[];
  /** core.llm 现在的完整内容（保存时在它上面改） */
  llm: ConfigValues;
  research: ConfigValues;
  state: ConfigState;
}

export interface ModelTestResult {
  ok: boolean;
  seconds: number;
  error?: string;
  model?: string;
  finish_reason?: string;
  content?: string;
  reasoning_chars?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  reasoning_tokens?: number;
}

export interface PluginInfo {
  id: string;
  exists: boolean;
  enabled: boolean;
  loaded: boolean;
  /** 加载了并且真的注册了东西（有的插件缺数据库时加载了但不起作用） */
  active: boolean;
  summary: string;
  description: string;
  config: string | null;
  commands: { name: string; aliases: string[]; description: string }[];
  tools: { name: string; description: string; parameters: unknown }[];
  keywords: string[];
  services: string[];
  hooks: { chat?: boolean; inbound?: number; outbound?: number; observers?: number };
}

export interface PluginsInfo {
  enabled: string[];
  default: string[];
  plugins: PluginInfo[];
  state: ConfigState;
}

// ---- 知识库 ----

export interface KbSummary {
  available: boolean;
  image_search: boolean;
  type_names: Record<string, string>;
  store: {
    chunks: number;
    with_vectors: number;
    fingerprint: string | null;
    loaded_at: string | null;
    average_chars: number;
    query_fingerprint: string | null;
    vectors_usable: boolean;
  } | null;
  images: {
    images: number;
    with_vectors: number;
    fingerprint: string | null;
    preprocess: string | null;
    loaded_at: string | null;
    min_score: number;
    sure_score: number;
    sure_margin: number;
  } | null;
  types: { type: string; chunks: number; docs: number; with_vectors: number; chars: number }[];
  image_kinds: { kind: string; images: number; with_vectors: number }[];
  settings: { top_k: number; keyword_slots: number } | null;
  /** 这个部署挂着的各个 wiki（core.wiki 的 wikis），排第一的是主 wiki */
  wikis: KbWiki[];
}

/** 挂着的一个 wiki：库里有它多少内容、它的自动更新跑得怎么样。 */
export interface KbWiki {
  id: string;
  /** 标出处时怎么称呼它 */
  label: string;
  chunks: number;
  with_vectors: number;
  fingerprint: string | null;
  loaded_at: string | null;
  images: number;
  /** 自动更新的状态；服务器上的 kbsync 服务还没跑过（或者本机调试）时是 null */
  sync: KbSync | null;
}

/** 自动更新的一轮（只有做了事或者出了问题的轮次才留记录）。 */
export interface KbSyncRound {
  at: string;
  /** unchanged 没有相关改动 / checked 对过了没变 / updated 内容有更新 / problem 有步骤没做成 */
  result: string;
  seconds?: number;
  /** 这一轮向 wiki 发了几个请求 */
  requests?: number;
  /** wiki 上新的改动，按种类数 */
  changes?: Record<string, number>;
  titles?: string[];
  problems?: string[];
  kb?: {
    full?: boolean;
    /** 重新渲染了几个页面，按原因分：revision / touched / data / refresh */
    renders?: Record<string, number>;
    /** 没取下来、沿用旧内容的 */
    stale?: number;
    /** 没取下来、也没有旧内容可用的（新建的页面），下一轮再取 */
    failed?: number;
    documents?: number;
    chunks?: number;
    added?: number;
    removed?: number;
    embedded?: number;
    upserted?: number;
    deleted?: number;
  };
  images?: {
    images?: number;
    stale?: number;
    downloaded?: number;
    embedded?: number;
    upserted?: number;
    deleted?: number;
  };
}

export interface KbSync {
  enabled: boolean;
  interval_seconds: number;
  checked_at: string | null;
  updated_at: string | null;
  /** 上次把渲染的页面全部重取 */
  full_at: string | null;
  images_at: string | null;
  /** 各部分没做成的原因；做成了是空字符串 */
  problems: { check: string; kb: string; images: string };
  pending: {
    kb: boolean;
    images: boolean;
    /** 等着按源码现渲染的页面 */
    fresh_titles: string[];
    /** 公共数据集最近一次改动的时间：等着全部重取 */
    shared_data_at: string | null;
    full_started_at: string | null;
  };
  rounds: KbSyncRound[];
}

export interface KbChunkRow {
  chunk_id: string;
  /** 出自哪个 wiki（core.wiki 里的 id）；分 wiki 之前导入的是空的 */
  wiki: string;
  doc_key: string;
  type: string;
  title: string;
  section_path: string[];
  seq: number;
  date: string | null;
  date_inferred: string | null;
  url: string;
  source: string;
  chars: number;
  preview: string;
  has_vector: boolean;
}

export interface KbChunk extends Omit<KbChunkRow, "preview" | "chars"> {
  text: string;
  entities: { name?: string; type?: string; certain?: boolean; via?: string }[];
  meta: Record<string, unknown>;
  fingerprint: string | null;
  updated_at: string;
  siblings: { chunk_id: string; seq: number; section_path: string[]; chars: number }[];
}

export interface KbSearchHit {
  chunk_id: string;
  wiki: string;
  type: string;
  title: string;
  section_path: string[];
  date: string | null;
  date_inferred: string | null;
  url: string;
  chars: number;
  text: string;
  /** 在向量、关键词两路里各排第几（从 1 数）；那一路没找到它是 null */
  vector_rank: number | null;
  keyword_rank: number | null;
}

export interface KbSearchResult {
  query: string;
  mode: string;
  vector_skipped: string | null;
  seconds: number;
  hits: KbSearchHit[];
}

export interface KbImage {
  image_id: string;
  wiki: string;
  kind: string;
  label: string;
  subject: string;
  page_title: string;
  url: string;
  image_url: string;
  text: string;
  has_vector: boolean;
}

export interface ImageSearchHit {
  image_id: string;
  wiki: string;
  label: string;
  kind: string;
  page_title: string;
  url: string;
  image_url: string;
  score: number;
  /** 认定就是这张图 */
  sure: boolean;
  text: string;
}

export interface Paged<T> {
  total: number;
  items: T[];
}

// ---- 会话、群聊 ----

export interface SessionRow {
  session_key: string;
  scope: string;
  chat_id: string;
  user_id: string;
  messages: number;
  turns: number;
  memories: number;
  traces: number;
  user_name: string | null;
  channel: string | null;
  last_at: string | null;
  chat_label: string;
  user_label: string;
}

export interface SessionMessage {
  id: number;
  role: string;
  created_at: string;
  content: string;
  tool_calls: { name: string | null; arguments: string | null }[];
  reasoning: boolean;
}

export interface SessionDetail {
  session_key: string;
  scope: string;
  chat_id: string;
  user_id: string;
  total_messages: number;
  messages: SessionMessage[];
  memories: { id: number; content: string; created_at: string }[];
  traces: TraceRow[];
  chat_label: string;
  user_label: string;
  user_name?: string | null;
}

/** 群聊清单里的一个群。条数、人数、最早一条都只算清单选的那段时间里的（选「全部」才是全部的）。 */
export interface ChatSummary {
  chat_id: string;
  scope: string;
  messages: number;
  addressed: number;
  members: number;
  first_at: string;
  last_at: string;
  /** 最近 24 小时的条数 */
  last_day: number;
  chat_label: string;
}

export interface ChatLogMessage {
  id: number;
  message_id: string;
  user_id: string;
  user_name: string | null;
  content: string;
  addressed: boolean;
  sent_at: string;
  attachments: number;
  user_label: string;
}

export interface Labels {
  chat: Record<string, string>;
  user: Record<string, string>;
}

// ---- 挂着的 wiki、站点目录 ----

/** 一个站点插件：哪来的、哪个版本 */
export interface SitePluginInfo {
  id: string;
  name: string;
  /** environment = 随部署装在环境里的；downloaded = 在这里下载的；local = 实例目录的 sites/ 下的；path = 按包路径写的 */
  source: "environment" | "downloaded" | "local" | "path";
  version: string | null;
}

/** 一个 wiki 的知识库在这台机器上的现状 */
export interface WikiKbState {
  /** none = 没有文件；requested = 点了建知识库、自动更新还没接手；building = 正在从头建；ready = 有了 */
  state: "none" | "requested" | "building" | "ready";
  requested_at: string | null;
  started_at: string | null;
  /** 上一轮没做成的原因 */
  problem: string;
}

export interface WikiInfo {
  id: string;
  label: string;
  /** 排第一的那个：对话、查证、人设用的是它的站点插件的补充 */
  primary: boolean;
  /** 在这一页里加的（能在这里移除）；不然是部署的配置文件里写的 */
  added: boolean;
  platform: string;
  site: SitePluginInfo | null;
  api_url: string;
  /** 请求要带的放行请求头的名字；不用放行是空的 */
  auth_header: string;
  auth_key_set: boolean;
  /** 正在运行的那一套里有没有它 */
  running: boolean;
  kb: WikiKbState;
}

export interface WikisInfo {
  writable: boolean;
  platforms: { id: string; name: string; description: string }[];
  wikis: WikiInfo[];
  /** 在这里加过、现在挂不上的和原因 */
  skipped: string;
  /** 建知识库靠的几样东西在不在 */
  kb: { plugin: boolean; embedding: boolean; sync_alive: boolean };
  catalog: { enabled: boolean; repo: string; error?: string };
  state: ConfigState;
}

/** 站点目录里的一项 */
export interface CatalogSite {
  id: string;
  platform: string;
  name: string;
  description: string;
  repo: string;
  /** 建议的 wiki 名字、出处里的叫法 */
  wiki_id: string;
  label: string;
  api_url: string;
  auth_header: string;
  /** 这个版本的核心用不了它的原因；能用是空的 */
  unavailable: string;
  /** 这个站点插件在不在这台机器上；不在的话添加时会下载 */
  installed: SitePluginInfo | null;
  /** 已经挂着的话，是哪个 wiki */
  wiki: string | null;
}

export interface CatalogInfo {
  enabled: boolean;
  repo: string;
  sites: CatalogSite[];
  error: string;
  problems?: string[];
  fetched_at?: number | null;
}
