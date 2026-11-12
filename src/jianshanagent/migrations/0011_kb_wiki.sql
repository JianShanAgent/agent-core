-- 一个部署可以挂好几个 wiki：切片和图片都标上出自哪个 wiki（core.wiki.yaml 的 wikis 里的 id）。
-- 库里的 chunk_id、doc_key、image_id 前面带着「<wiki>:」，两个 wiki 的页面号、文件名重了也不冲突。
-- 这次迁移之前导入的行没有出处（wiki = ''）：照常能查，主 wiki 下一次导入时整批换成带出处的。
ALTER TABLE kb_chunks ADD COLUMN wiki TEXT NOT NULL DEFAULT '';
ALTER TABLE kb_images ADD COLUMN wiki TEXT NOT NULL DEFAULT '';
CREATE INDEX kb_chunks_wiki_idx ON kb_chunks (wiki);

-- kb_meta 里的概况和自动更新的状态按 wiki 分开存：键是「stats:<wiki>」「sync:<wiki>」。
-- 原来的概况算在「没有出处」名下；原来的更新状态不留，下一轮自动更新会写上新的。
UPDATE kb_meta SET key = 'stats:' WHERE key = 'stats';
DELETE FROM kb_meta WHERE key = 'sync';
