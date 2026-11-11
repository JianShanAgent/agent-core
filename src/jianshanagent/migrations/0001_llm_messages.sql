-- 多轮对话历史：按 LLM 接口的原始消息格式整条保存。
-- message 里保留 reasoning_content 与 tool_calls，思考模式下的多轮工具调用需要原样带回。
CREATE TABLE llm_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_key TEXT        NOT NULL,
    role        TEXT        NOT NULL,
    message     JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX llm_messages_session_idx ON llm_messages (session_key, id DESC);
