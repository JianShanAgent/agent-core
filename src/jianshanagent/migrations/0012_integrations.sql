-- 第三方应用及独立用量账本。不扫描历史 trace，也不存提示词、回复或明文 token。
CREATE TABLE integration_apps (
    app_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_digest BYTEA UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT true CHECK (NOT enabled OR token_digest IS NOT NULL),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_request_at TIMESTAMPTZ
);

CREATE TABLE integration_requests (
    request_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES integration_apps (app_id),
    started_at TIMESTAMPTZ NOT NULL,
    UNIQUE (app_id, request_id)
);
CREATE INDEX integration_requests_started_at_idx ON integration_requests (started_at);
CREATE INDEX integration_requests_app_started_at_idx ON integration_requests (app_id, started_at);

CREATE TABLE integration_calls (
    call_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('llm', 'embedding', 'search')),
    model TEXT NOT NULL,
    finished_at TIMESTAMPTZ,
    prompt_tokens BIGINT CHECK (prompt_tokens >= 0),
    completion_tokens BIGINT CHECK (completion_tokens >= 0),
    cached_tokens BIGINT CHECK (cached_tokens >= 0),
    reasoning_tokens BIGINT CHECK (reasoning_tokens >= 0),
    embedding_tokens BIGINT CHECK (embedding_tokens >= 0),
    search_tokens BIGINT CHECK (search_tokens >= 0),
    searches BIGINT CHECK (searches >= 0),
    FOREIGN KEY (app_id, request_id) REFERENCES integration_requests (app_id, request_id)
);
CREATE INDEX integration_calls_app_request_idx ON integration_calls (app_id, request_id);
