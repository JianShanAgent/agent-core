-- 备注名：平台给的群和用户标识是一长串看不懂的字符，在管理后台里可以给它们起个名字。
-- 只是给人看的，机器人处理消息不读这张表。
CREATE TABLE admin_labels (
    kind       TEXT        NOT NULL,           -- chat = 会话（群，或单聊的对方）；user = 群里的某个成员
    id         TEXT        NOT NULL,           -- 平台给的标识
    label      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, id)
);
