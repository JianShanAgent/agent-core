-- 群聊记录：机器人在群里收到的每一条消息，留着以后做分析、出日报。机器人回答问题不读这张表。
-- 能收到哪些由 QQ 那边决定：群主让机器人「获取群内全部消息」的群是每一条发言，其余的群只有 @ 机器人的。
CREATE TABLE chat_messages (
    id         BIGSERIAL PRIMARY KEY,
    message_id TEXT        NOT NULL UNIQUE,  -- 平台的消息 id；断线续接时平台会把消息重发一遍，重复的不再记
    scope      TEXT        NOT NULL,         -- 会话类型，目前只有 group
    chat_id    TEXT        NOT NULL,         -- 群的标识（openid）
    user_id    TEXT        NOT NULL,         -- 发言人在这个群里的标识（openid）
    user_name  TEXT,                         -- 发言人的昵称，平台给了才有
    content    TEXT        NOT NULL,         -- 文字。@ 别人的写成「@昵称」，@ 机器人的标记已经去掉
    addressed  BOOLEAN     NOT NULL,         -- 是不是对机器人说的（@ 了机器人，而且有文字或图片）
    sent_at    TIMESTAMPTZ NOT NULL,         -- 发言时间（平台给的；没给就是收到的时间）
    raw        JSONB       NOT NULL,         -- 平台事件原文：附件、被 @ 的人、引用了哪条都在里面；鉴权令牌已去掉
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX chat_messages_chat_time_idx ON chat_messages (chat_id, sent_at);
