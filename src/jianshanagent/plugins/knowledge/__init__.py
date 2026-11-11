"""knowledge 插件：wiki 知识库。

离线部分（命令行，``python -m jianshanagent.plugins.knowledge --help``）：

- ``run``    取数 → 清洗 → 转 Markdown → 切片 → 去重 → 输出切片文件和质量报告
- ``embed``  给切片算向量，存成向量文件
- ``load``   把切片和向量导入数据库
- ``images collect`` / ``images embed`` / ``images load``  wiki 图片索引（以图搜图用）的同样三步：
  选图并下载原图 → 给图片算向量 → 导入数据库

运行时部分（plugin.py）：把检索注册成服务，查证工作流（research 插件）拿它做第一级取证；
以图搜图也注册成服务（``image_search``，见 images.py），用户发图问「这是谁」时用。
"""

from jianshanagent.plugins.knowledge.plugin import KnowledgePlugin, create_plugin

__all__ = ["KnowledgePlugin", "create_plugin"]
