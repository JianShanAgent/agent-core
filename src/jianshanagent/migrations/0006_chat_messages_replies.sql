-- 群聊记录里也记机器人自己的回复；加上按人删除、按时间清理要用的索引。
ALTER TABLE chat_messages
    ADD COLUMN from_bot BOOLEAN NOT NULL DEFAULT false,  -- 这一行是机器人发的回复（user_id 固定写 bot）
    ADD COLUMN reply_to TEXT;                            -- 机器人的回复：回的是哪条消息（那条的 message_id）

CREATE INDEX chat_messages_user_idx ON chat_messages (user_id);    -- 用户要求删除他的数据时按人找
CREATE INDEX chat_messages_reply_idx ON chat_messages (reply_to) WHERE reply_to IS NOT NULL;
CREATE INDEX chat_messages_sent_idx ON chat_messages (sent_at);    -- 过期清理按时间找
