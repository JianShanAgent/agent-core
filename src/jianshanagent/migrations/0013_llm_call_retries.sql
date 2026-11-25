-- 模型调用的自动重发次数，不含首次请求。旧记录没有追踪，不补成 0。
ALTER TABLE llm_calls ADD COLUMN retry_count INT CHECK (retry_count >= 0);
