from deployment import FULL_CONFIG_DIR
from jianshanagent.core.config import load_config


def test_load_repo_config() -> None:
    root = FULL_CONFIG_DIR
    cfg = load_config(root)
    assert cfg.app["name"] == "jianshanagent"
    assert "wiki_query" in (cfg.get("core.plugins").get("enabled") or [])
    # 服务哪个 wiki、加载哪些站点插件，在 core.wiki 里；站点插件的补充读配置时就并进通用配置了
    # （这份测试配置是纯 MediaWiki；带站点插件的样子在各个站点插件仓库的测试里）
    assert cfg.get("core.wiki")["name"] == "示例" and cfg.sites == []
