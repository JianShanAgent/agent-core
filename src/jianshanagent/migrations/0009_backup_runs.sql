-- 每次数据库备份的结果。写的是备份容器（deploy/backup/backup.sh，每次备份完记一行），
-- 读的是管理后台：显示最近一次备份，一天多没有成功的就提醒。机器人处理消息不读这张表。
-- 只留 90 天（备份脚本每次顺手删掉更早的）。
CREATE TABLE backup_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN     NOT NULL,            -- 数据库那份备份成没成
    file        TEXT        NOT NULL DEFAULT '', -- 备份文件名（失败时是空的）
    bytes       BIGINT      NOT NULL DEFAULT 0,
    copied      BOOLEAN     NOT NULL DEFAULT false, -- 有没有另存一份到第二个存放处
    detail      TEXT        NOT NULL DEFAULT ''  -- 哪里出了问题；一切正常时是空的
);
CREATE INDEX backup_runs_finished_at ON backup_runs (finished_at DESC);
