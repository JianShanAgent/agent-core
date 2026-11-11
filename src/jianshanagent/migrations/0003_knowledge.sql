-- wiki 知识库：切片、向量和关键词（由 python -m jianshanagent.plugins.knowledge load 写入）。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE kb_chunks (
    chunk_id      TEXT PRIMARY KEY,
    doc_key       TEXT        NOT NULL,
    type          TEXT        NOT NULL,           -- entry / announcement / story / data
    title         TEXT        NOT NULL,
    section_path  TEXT[]      NOT NULL DEFAULT '{}',
    seq           INT         NOT NULL DEFAULT 0,
    text          TEXT        NOT NULL,           -- 首行是标题路径，后面是正文（Markdown）
    url           TEXT        NOT NULL DEFAULT '',
    source        TEXT        NOT NULL DEFAULT '',
    date          TEXT,                           -- 页面或公告自己写明的日期 YYYY-MM-DD
    date_inferred TEXT,                           -- 推断出来的年月 YYYY-MM
    entities      JSONB       NOT NULL DEFAULT '[]',
    meta          JSONB       NOT NULL DEFAULT '{}',
    -- 关键词检索用的词（见 plugins/knowledge/terms.py），不重复
    terms         TEXT[]      NOT NULL DEFAULT '{}',
    -- 向量和它的指纹（提供方 + 型号 + 维度）。指纹和查询用的不一致时不走向量检索
    embedding     vector(4096),
    fingerprint   TEXT,
    -- 这一行内容的哈希：重新导入时没变的行不动
    row_hash      TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX kb_chunks_terms_idx ON kb_chunks USING gin (terms);
CREATE INDEX kb_chunks_doc_idx ON kb_chunks (doc_key, seq);

-- 每个词出现在多少个切片里（每次导入后整表重算），查询时用来算权重
CREATE TABLE kb_terms (
    term TEXT PRIMARY KEY,
    df   INT NOT NULL
);

-- 知识库的概况：切片数、向量指纹、导入时间
CREATE TABLE kb_meta (
    key   TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
