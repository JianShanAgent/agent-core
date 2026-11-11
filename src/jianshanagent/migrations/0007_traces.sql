-- 处理记录和模型用量（管理后台的统计、「查证过程」从这两张表来；写入见 core/trace_store.py）。

-- 一条对机器人说的消息从进到出：走了哪条路、用了多久、回了什么、中间每一步的结果。
CREATE TABLE traces (
    id                TEXT PRIMARY KEY,                -- 按时间排得出先后的编号（20261111-174501-a1b2c3）
    started_at        TIMESTAMPTZ NOT NULL,
    seconds           REAL        NOT NULL,            -- 从收到到处理完用了多久
    channel           TEXT        NOT NULL,            -- qq / web（管理后台的网页对话）
    scope             TEXT        NOT NULL,            -- c2c / group …
    chat_id           TEXT        NOT NULL,
    user_id           TEXT        NOT NULL,
    user_name         TEXT,
    session_key       TEXT        NOT NULL,
    message_id        TEXT        NOT NULL,
    question          TEXT        NOT NULL,            -- 用户的话
    images            INT         NOT NULL DEFAULT 0,  -- 带了几张图
    route             TEXT        NOT NULL,            -- command / chat / research / keyword / session / limited / dropped / none
    command           TEXT        NOT NULL DEFAULT '', -- route 是 command 时：哪个命令
    status            TEXT        NOT NULL,            -- replied / silent / error
    outcome           TEXT        NOT NULL DEFAULT '', -- 查证的结果：answered / not_found / failed；没走查证是空的
    reply             TEXT        NOT NULL DEFAULT '', -- 最终发给用户的回复
    error             TEXT        NOT NULL DEFAULT '',
    llm_calls         INT         NOT NULL DEFAULT 0,
    prompt_tokens     INT         NOT NULL DEFAULT 0,
    completion_tokens INT         NOT NULL DEFAULT 0,  -- 含思考
    reasoning_tokens  INT         NOT NULL DEFAULT 0,
    steps             JSONB       NOT NULL DEFAULT '[]', -- 过程：每一步的种类、开始时刻、用时、结果
    summary           JSONB       NOT NULL DEFAULT '{}'  -- 补充信息（查证的概况等）
);

CREATE INDEX traces_started_idx ON traces (started_at DESC);
CREATE INDEX traces_session_idx ON traces (session_key, started_at DESC);

-- 每一次模型调用。不属于任何一条消息的调用（管理后台里测试模型、后台任务）trace_id 是空的。
CREATE TABLE llm_calls (
    id                BIGSERIAL PRIMARY KEY,
    trace_id          TEXT,                            -- 属于哪条处理记录（traces.id）
    step_id           INT,                             -- 属于那条记录的第几步
    started_at        TIMESTAMPTZ NOT NULL,
    seconds           REAL        NOT NULL,
    purpose           TEXT        NOT NULL DEFAULT '', -- 哪个环节调的：entry / analyze / analyst / review / fix / web …
    profile           TEXT        NOT NULL,            -- core.llm.yaml 里的档位
    model             TEXT        NOT NULL,
    thinking          BOOLEAN     NOT NULL,
    ok                BOOLEAN     NOT NULL,            -- 接口有没有正常返回（超时被掐掉、报错都算没有）
    finish_reason     TEXT        NOT NULL DEFAULT '',
    error             TEXT        NOT NULL DEFAULT '',
    prompt_tokens     INT         NOT NULL DEFAULT 0,
    completion_tokens INT         NOT NULL DEFAULT 0,  -- 含思考
    reasoning_tokens  INT         NOT NULL DEFAULT 0,
    cached_tokens     INT         NOT NULL DEFAULT 0,
    tool_calls        INT         NOT NULL DEFAULT 0,
    -- 发给模型的内容和它的回复（图片换成了占位）。过了保留期清空，上面的数字留着
    request           JSONB,
    response          JSONB
);

CREATE INDEX llm_calls_started_idx ON llm_calls (started_at DESC);
CREATE INDEX llm_calls_trace_idx ON llm_calls (trace_id);
