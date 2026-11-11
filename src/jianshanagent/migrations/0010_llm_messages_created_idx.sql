-- 管理后台的「用户与会话」清单默认只列最近 30 天有动静的会话，要按时间找对话历史里最近的行。
-- 这张表只增不减，没有这个索引的话，页面每刷新一次都要把整张表读一遍。
CREATE INDEX llm_messages_created_idx ON llm_messages (created_at);
