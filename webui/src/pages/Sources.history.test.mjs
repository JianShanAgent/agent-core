import assert from "node:assert/strict";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// 用实际页面模块/React 服务端渲染测试，不往生产界面注入演示状态。
const root = fileURLToPath(new URL("../../", import.meta.url));
const server = await createServer({ root, server: { middlewareMode: true }, appType: "custom" });
after(() => server.close());
const ui = await server.ssrLoadModule("/src/pages/Sources.tsx");
const { api } = await server.ssrLoadModule("/src/lib/api.ts");
const legacy = {
  uid: "123456", enabled: false, initial_days: 30, overlap_days: 7,
  max_posts: 200, interval_seconds: 1800, include_reposts: true,
};
const full = {
  ...legacy, mode: "full_then_incremental", include_comments: true,
  include_replies: true, discussion_source_id: "test_discussion",
};
const renderProgress = (status) => renderToStaticMarkup(createElement(ui.HistoryProgress, { status, intervalSeconds: 1800 }));

test("旧配置编辑保留mode缺省，首次启用全历史只需确认一次范围", () => {
  assert.equal(ui.collectorProblem(legacy), "");
  assert.equal(ui.collectorConfirmation(legacy, legacy), null);
  assert.equal(Object.hasOwn(legacy, "mode"), false);
  const scope = ui.collectorConfirmation(full, legacy);
  assert.match(scope, /全部可访问历史帖子/);
  assert.match(scope, /评论/);
  assert.match(scope, /楼中楼/);
  assert.match(scope, /test_discussion/);
  assert.match(scope, /后续有界批次自动续采，不逐批确认/);
  assert.equal(ui.collectorConfirmation(full, full), null);
  assert.match(ui.collectorConfirmation({ ...full, enabled: true }, full), /开启定时/);
});

test("讨论范围和来源身份约束", () => {
  assert.match(ui.collectorProblem({ ...full, include_comments: false }), /楼中楼/);
  assert.match(ui.collectorProblem({ ...full, discussion_source_id: "../bad" }), /分区 ID/);
  assert.match(ui.collectorProblem(full, "test_discussion"), /不能使用主来源/);
  assert.match(ui.collectorProblem({ ...full, uid: "" }), /目标主页/);
  assert.equal(ui.collectorProblem(full, "test_parent"), "");
});

test("最近窗口和缺省旧模式不可保存评论范围，校验提示且不静默切换全历史", () => {
  for (const mode of [undefined, "recent"]) {
    const config = { ...full, mode };
    assert.match(ui.collectorProblem(config), /需先选择「全部历史 → 核验后增量」模式/);
    const html = renderToStaticMarkup(createElement(ui.CollectorFields, {
      value: config, onChange() { assert.fail("渲染不应静默改动模式"); }, disabled: false,
    }));
    assert.match(html, /role="alert"[^>]*>采集评论及楼中楼需先选择/);
    assert.match(html, /最近窗口不支持/);
    assert.equal(config.mode, mode);
  }
  assert.equal(ui.collectorProblem({ ...legacy, mode: "recent" }), "");
  assert.equal(ui.collectorProblem(full), "");
});

test("仅显式历史完成且已切incremental才展示30分钟增量", () => {
  for (const status of [undefined, { state: "completed" }, { state: "completed", phase: "completed" },
    { history_complete: true, history_phase: "verifying" }, { history_phase: "incremental" },
    { history_complete: false, history_phase: "incremental" }]) {
    assert.equal(ui.historyIsIncremental(status), false);
    const html = renderProgress(status);
    assert.match(html, /尚未进入增量/);
    assert.doesNotMatch(html, /全历史已核验完成|每 30 分钟增量|增量采集中|7000|7,000|%/);
  }
  const done = { history_complete: true, history_phase: "incremental" };
  assert.equal(ui.historyIsIncremental(done), true);
  assert.match(renderProgress(done), /全历史已核验完成 · 每 30 分钟增量/);
});

test("历史进度使用累计提交数、真实时间及待处理/差异/资源，不把缺失当0", () => {
  const html = renderProgress({
    history_phase: "blocked", history_complete: false, posts_total: 123, comments_total: 456,
    replies_total: 78, pending_posts: 9, pending_comments: 10, pending_replies: 11,
    index_pending: 12, inaccessible: 13, count_mismatches: 14,
    oldest_date: "2020-01-01T00:00:00Z", snapshot_at: "2026-01-01T00:00:00Z",
    resource_bytes: 2 * 1024 ** 3, vector_bytes: 128 * 1024 ** 2,
    coverage_message: "索引容量达到资源预算，扩容前保留断点", measurement: { peak_rss_bytes: 1024 },
  });
  for (const text of ["累计提交历史帖子", "123", "累计提交评论", "456", "累计提交楼中楼", "78",
    "待处理帖子 9", "待处理评论 10", "待处理楼中楼 11", "待索引 12", "数量差异 14", "无法访问 / 未能访问 13",
    "2020", "2026", "2.00 GiB", "128.00 MiB", "索引容量达到资源预算", "peak_rss_bytes", "历史仍未完成"]) assert.ok(html.includes(text), text);
  assert.match(renderProgress({}), /累计提交历史帖子 <strong>—<\/strong>/);
  assert.doesNotMatch(renderProgress({ posts_total: -1, comments_total: NaN }), /NaN|<strong>-1/);
});

test("全部已定义历史阶段和暂停可安全展示，未知历史阶段不误报完成", () => {
  for (const phase of ["posts", "comments", "indexing", "verifying", "paused", "blocked"]) {
    const html = renderProgress({ history_phase: phase });
    assert.match(html, /尚未进入增量/);
    assert.doesNotMatch(html, /等待服务端报告历史阶段/);
  }
  assert.match(renderProgress({ paused: true, history_phase: "posts" }), /历史采集已暂停/);
  assert.match(renderProgress({ history_phase: "future_phase" }), /等待服务端报告历史阶段/);
  assert.match(renderProgress({ state: "completed" }), /旧窗口或本批完成记录不能作为全历史完成证据/);
});

test("全历史字段隐藏无效首次天数，同时旧配置入口仍有窗口设置", () => {
  const render = (value) => renderToStaticMarkup(createElement(ui.CollectorFields, { value, onChange() {}, disabled: false }));
  assert.match(render(legacy), /首次回溯（天）/);
  assert.match(render(legacy), /采集模式/);
  assert.doesNotMatch(render(full), /首次回溯（天）/);
  assert.match(render(full), /历史完成后的增量周期/);
  assert.match(render(full), /社区讨论分区 ID/);
});

test("主来源与community讨论按discussion_of分组，孤立关联也不消失", () => {
  const parent = { id: "parent", platform: "weibo", role: "official" };
  const child = { id: "child", platform: "weibo", role: "community", discussion_of: "parent" };
  const orphan = { id: "orphan", platform: "weibo", discussion_of: "missing" };
  const groups = ui.groupWeiboSources([child, { id: "wiki", platform: "wiki" }, parent, orphan]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].source, parent);
  assert.deepEqual(groups[0].discussions, [child]);
  assert.equal(groups[1].source, orphan);
  assert.equal(parent.role, "official");
});

test("讨论子来源不会挂载CollectorLine或产生独立时间线控制，非微博不展示全历史", () => {
  const props = { locked: false, onToggleMounted() {}, onOpenImport() {}, onRecover() {}, onEdit() {} };
  const source = { id: "test_discussion", label: "讨论", platform: "weibo", mounted: true, role: "community",
    discussion_of: "test_parent", collector: full, import_status: null, knowledge: null };
  // 没有QueryClientProvider；若误挂载CollectorLine，其useQuery会使测试直接失败。
  const html = renderToStaticMarkup(createElement(ui.WeiboSourceCard, { ...props, source }));
  assert.match(html, /社区讨论分区/);
  assert.match(html, /保留真实作者/);
  assert.doesNotMatch(html, /暂停历史|继续采集|立即采集|全历史采集进度/);
  const other = renderToStaticMarkup(createElement(ui.WeiboSourceCard, { ...props, source: { ...source, platform: "other", discussion_of: null } }));
  assert.doesNotMatch(other, /暂停历史|继续采集|全历史采集进度/);
});

test("主来源历史批次完成不冒充全历史完成；跨批可暂停，暂停后可继续，提交阶段不可停", () => {
  const render = (status) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const source = { id: "test_parent", label: "正文", platform: "weibo", mounted: true, role: "official",
      collector: full, collector_status: status.state, collection: status, import_status: null, knowledge: null };
    return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(ui.WeiboSourceCard, {
      source, locked: false, onToggleMounted() {}, onOpenImport() {}, onRecover() {}, onEdit() {},
    })));
  };
  const batch = render({ state: "completed", phase: "completed", history_phase: "posts", job_id: "batch-job" });
  assert.match(batch, /本批完成 · 历史未完成/);
  assert.match(batch, /尚未进入增量/);
  assert.match(batch, /暂停历史/);
  assert.match(batch, /继续采集/);
  assert.match(batch, /以下仅为本批/);
  assert.doesNotMatch(batch, /采集完成|全历史已核验完成|每 30 分钟增量/);
  const paused = render({ state: "completed", phase: "completed", history_phase: "paused", paused: true, job_id: "batch-job" });
  assert.match(paused, /历史采集已暂停/);
  assert.match(paused, /继续采集/);
  assert.doesNotMatch(paused, />暂停历史<|本批完成 · 历史未完成/);
  const committing = render({ state: "committing", phase: "committing", history_phase: "indexing", job_id: "batch-job" });
  assert.match(committing, /<button[^>]*disabled=""[^>]*>暂停历史<\/button>/);
  assert.match(committing, /正在安全提交，暂不能停止/);
});

test("请求保留旧配置，并通过现有路由绑定job_id暂停/空body恢复，full显式stream_v2", async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body ?? "null") });
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    await api.updateSource("test_parent", { collector: legacy });
    assert.deepEqual(requests.at(-1).body, { collector: legacy });
    await api.updateSource("test_parent", { collector: full });
    assert.equal(requests.at(-1).body.storage, "stream_v2");
    assert.equal(requests.at(-1).body.collector.discussion_source_id, "test_discussion");
    await api.createSource({ id: "test_parent", collector: full });
    assert.equal(requests.at(-1).body.storage, "stream_v2");
    await api.collectSource("test_parent");
    assert.equal(requests.at(-1).url, "/api/sources/test_parent/collect");
    assert.deepEqual(requests.at(-1).body, {});
    await api.cancelSourceCollection("test_parent", "current-job");
    assert.equal(requests.at(-1).url, "/api/sources/test_parent/collect/cancel");
    assert.deepEqual(requests.at(-1).body, { job_id: "current-job" });
    assert.equal(requests.length, 5);
  } finally { globalThis.fetch = original; }
});
