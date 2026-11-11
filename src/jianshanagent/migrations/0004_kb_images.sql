-- wiki 图片索引：以图搜图用（由 python -m jianshanagent.plugins.knowledge images load 写入）。
-- 几千行，不建向量索引：整表精确比，不会漏。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_images (
    image_id    TEXT PRIMARY KEY,               -- wiki 上的文件名（Skin_330_03.png）
    kind        TEXT        NOT NULL,           -- 式神立绘 / 皮肤立绘 / 式神头像 / 御魂图标 …（中文，直接给人看）
    label       TEXT        NOT NULL,           -- 给人看的一句话：不知火的皮肤「蝶步韶华」
    -- 画的是什么：同一个造型的几张图（立绘大图、卡面小图…）写的是同一个值，检索结果里每个只留最像的一张
    subject     TEXT        NOT NULL DEFAULT '',
    page_title  TEXT        NOT NULL DEFAULT '', -- 所属的 wiki 页面
    url         TEXT        NOT NULL DEFAULT '', -- 那个页面的链接
    image_url   TEXT        NOT NULL DEFAULT '',
    text        TEXT        NOT NULL DEFAULT '', -- 给大模型当资料的说明：这是谁的什么图、皮肤名、获取方式…
    meta        JSONB       NOT NULL DEFAULT '{}',
    sha1        TEXT        NOT NULL DEFAULT '', -- 原图内容的哈希（wiki 给的）：wiki 上换了图它就变
    -- 向量，以及它是怎么来的：指纹（提供方 + 型号 + 维度，和文本知识库的一样）和算向量前对图片做的预处理。
    -- 查询图片的向量这两样都和库里的一致才能比
    embedding   vector(4096),
    fingerprint TEXT,
    preprocess  TEXT,
    -- 这一行内容的哈希：重新导入时没变的行不动
    row_hash    TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
