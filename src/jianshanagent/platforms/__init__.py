"""核心带着的 wiki 平台：一个平台在 MediaWiki 之上多出来的东西。

通用的代码只懂 MediaWiki。挂着的 wiki 在哪个平台上，就多加载哪个平台的这一份
（``core.wiki.yaml`` 里那个 wiki 的 ``platform``，或者跟着它的站点插件走）：

- ``huiji``：灰机 wiki（huijiwiki.com）。Data 命名空间和它的查询接口、灰机约定的清洗规则；
- ``bwiki``：BWiki（wiki.biligame.com）。语义查询、取数的速率、平台组件的清洗规则；
- ``mediawiki``：自己架的 MediaWiki 站。什么都不多带。

一个平台是这里的一个目录：``platform.yaml``（清单）、``config.yaml``（默认配置，叫
``platform.<id>``，里面的 ``extend`` 段是对通用配置的补充）、接扩展点的模块（``wiki.py``、
``knowledge.py``，各有 ``create(setup)``）。接法和站点插件一样，见 ``core/manifest.py``、
``core/sites.py``。具体某个站的东西不在这里，在它的站点插件里。
"""
