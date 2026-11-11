-- 用户记忆：scope 是会话键（单聊按用户，群聊按「群 + 群成员」）。
CREATE TABLE memories (
    id         BIGSERIAL PRIMARY KEY,
    scope      TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, content)
);
