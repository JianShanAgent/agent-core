<p align="center"><img src="assets/icon.svg" width="96" alt="JianShanAgent"></p>

# agent-core

> JianShanAgent · 核心 · 给 MediaWiki 站点用的 QQ 问答机器人（见山，取自「开门见山」）
>
> A QQ question-answering bot for MediaWiki sites: direct wiki lookup, retrieval-augmented answers with a review step, reverse image search, and an admin console. This is the core of JianShanAgent.

| | |
|---|---|
| 层 | 核心 |
| id | — |
| 包 | 发行包 `jianshanagent` / Python 包 `jianshanagent` |
| 依赖 | Python 3.12+；Postgres（pgvector）；QQ 机器人、对话模型、嵌入接口的凭据 |
| 登记 | 别的包往这两个 entry points 组里登记插件：`jianshanagent.plugins`（功能插件）、`jianshanagent.sites`（站点插件） |

## 这是什么

一个挂在 QQ 上的 wiki 问答机器人。精确命令（`/查`）直查 MediaWiki，不过模型；自然语言问题走查证工作流：
分析问题 → 查知识库和站内搜索 → 复核 → 回答并附参考资料。用户发来的图片可以在 wiki 的图库里以图搜图。
带一个管理后台（网页），看每条消息怎么处理的、改配置、开关插件。

核心只懂 MediaWiki 和 QQ。某个 wiki 平台、某个站特有的东西（数据接口、清洗规则、提示词里的说法、人设）
由**站点插件**带进来；日志采集这类可拆的服务是**部署插件**；把它们拼起来指向某一个 wiki 的是**实例**。

## 在整套里的位置

JianShanAgent 由几个仓库拼成（并排放在开发机的同一个目录下）：

| 仓库 | 层 | 管什么 |
|---|---|---|
| **[`agent-core`](https://github.com/JianShanAgent/agent-core)（这里）** | 核心 | 机器人本体：QQ 网关、七个功能插件（查询、知识库、查证、对话、记忆、群聊记录、安全兜底）、管理后台、部署工具 |
| [`site-huiji`](https://github.com/JianShanAgent/site-huiji) | 站点插件 · 平台 | 灰机 wiki 平台在 MediaWiki 之上多出来的东西 |
| [`site-huiji-yys`](https://github.com/JianShanAgent/site-huiji-yys) | 站点插件 · 站 | 阴阳师 wiki 特有的东西（建在灰机平台上） |
| [`instance-yys`](https://github.com/JianShanAgent/instance-yys) | 实例 | 阴阳师 wiki 机器人这个部署：配置、用哪个版本、运维备忘 |
| [`template-site-platform`](https://github.com/JianShanAgent/template-site-platform) | 模板 · 站点插件 · 平台 | 接一个新的 wiki 平台时，从它开始 |
| [`template-site-wiki`](https://github.com/JianShanAgent/template-site-wiki) | 模板 · 站点插件 · 站 | 接一个新的 wiki 时，从它开始 |
| [`template-instance`](https://github.com/JianShanAgent/template-instance) | 模板 · 实例 | 新建一个部署时，从它开始 |

## 目录

```
src/jianshanagent/
  core/        配置、消息模型、插件宿主、路由、模型和嵌入的调用、追踪
  gateway/     QQ 接入（qqbot-agent-sdk）
  plugins/     自带的功能插件，一个一个目录：清单 plugin.yaml、默认配置 config.yaml、代码
  admin/       管理后台的接口（页面的源码在 webui/）
  defaults/    核心的默认配置：app、core.*、gateway.*
  testing/     写插件测试时用的东西、插件规范自检
  migrations/  数据库结构的变更，机器人启动时自动执行
deploy/        部署工具：Dockerfile、compose、发布脚本、备份；plugins/ 下是部署插件（logs = 运行日志）
webui/         管理后台的页面（React）
config/        空的部署目录，本机试跑核心时用（默认值不在这里，见「配置」）
docs/          文档
tests/
```

## 安装和启用

核心不单独部署，总是和一个实例一起用。新建一个部署：用模板 [`template-instance`](https://github.com/JianShanAgent/template-instance)
建一个实例仓库，在它的 `config/core.wiki.yaml` 里写上 wiki 的地址，`components.lock` 里列出要用的仓库，
然后按 [部署与运维](docs/部署与运维.md) 发布。要接这个 wiki 特有的东西，用两个站点插件的模板。

本机试跑（不连 QQ，纯 MediaWiki）：

```bash
uv sync --extra dev
cp config/secrets.example.yaml config/secrets.yaml          # 填对话模型等凭据
JIANSHANAGENT_DRY_RUN=1 uv run jianshanagent                  # 还要在 config/core.wiki.yaml 里写 api_url
```

同一个机器人不能两处同时在线（一条消息会被回复两次）：线上的在跑时，开发机上只用 `JIANSHANAGENT_DRY_RUN=1`。

## 配置

统一 YAML，分层，后面的盖住前面的（细节见 `src/jianshanagent/core/config.py` 开头）：

| 层 | 在哪 | 说明 |
|---|---|---|
| 默认值 | `src/jianshanagent/defaults/*.yaml`、各插件目录的 `config.yaml` | 随代码走。配置名：`app`、`core.*`、`gateway.*`、`plugin.<id>`、`site.<id>` |
| 站点插件的补充 | 站点插件 `config.yaml` 的 `extend` 段 | 这个站对通用配置的补充，读配置时并进去 |
| 部署的改动 | 实例仓库的 `config/<配置名>.yaml` | 只写和默认值不一样的项；`core.wiki.yaml`（服务哪个 wiki）就在这里 |
| 开发机的调试覆写 | `config/local/` | 不入库、不上传 |
| 管理后台里改的 | 数据盘上的覆写目录（`JIANSHANAGENT_CONFIG_OVERRIDES_DIR`） | 只记改过的项，换发布还在 |

凭据在 `config/secrets.yaml`（不入库），模板是 `config/secrets.example.yaml`。

## 开发和测试

```bash
uv sync --extra dev
uv run pytest            # 核心自己的测试（纯 MediaWiki，不带站点插件）
uv run ruff check . && uv run ruff format --check .
```

Postgres 相关的测试默认跳过，设 `JIANSHANAGENT_TEST_POSTGRES_DSN`（本机测试库）后运行。
查询和知识库管线的大部分测试是拿阴阳师 wiki 的数据写的，在 `site-huiji-yys` 里；改了这两块，那边的测试也要跑。
管理后台的页面：`cd webui && npm install && npm run build`（见 [管理后台](docs/管理后台.md)）。

## 相关文档

- [架构](docs/架构.md)：核心里有什么、一条消息怎么走、数据放在哪、写代码时要知道的约束
- [插件规范](docs/插件规范.md)：分层和命名、插件的目录和清单、怎么登记、文档的格式、规范自检、换一个 wiki
- [部署与运维](docs/部署与运维.md)：发布、回滚、知识库自动更新、备份和恢复、新服务器
- [管理后台](docs/管理后台.md)
- 许可：[MIT](LICENSE)
