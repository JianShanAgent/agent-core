# config/

部署目录的样子。**默认值不在这里**，在包里（`src/jianshanagent/defaults/` 和各插件目录的 `config.yaml`）。一个部署的 `config/` 只放它和默认值不一样的地方：

| 文件 | 用途 |
|---|---|
| `<配置名>.yaml` | 对同名默认配置的改动，只写要改的项（如 `core.wiki.yaml`：服务哪个 wiki） |
| `secrets.yaml` | 凭据，不入库（模板是 `secrets.example.yaml`） |
| `local/<配置名>.yaml` | 开发机的调试覆写，不入库、不上传 |

这个仓库里的 `config/` 是空的（全用默认值），本机试跑核心时用；真正的部署见各个实例仓库（`instance-*`）。
