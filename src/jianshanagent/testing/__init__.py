"""写插件测试时用的东西，随核心一起发出去（插件住在别的仓库里，测试要用到这些）。

- :mod:`jianshanagent.testing.sites`：在临时目录里搭站点插件、把包登记成插件；
- :mod:`jianshanagent.testing.conformance`：插件合不合规范的自检，每个插件仓库的测试里调一次。

依赖 pytest，只在测试里导入。
"""
