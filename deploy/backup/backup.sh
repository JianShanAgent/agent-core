#!/usr/bin/env bash
# Postgres 备份。由 compose 的 backup 服务运行，也可以手动执行：
#   ctl exec backup bash /usr/local/bin/backup.sh once             立即备份一次
#   ctl exec backup bash /usr/local/bin/backup.sh check            最近是否有成功的备份
#   ctl exec backup bash /usr/local/bin/backup.sh restore <文件名>  用一份备份恢复数据库（见下面）
#
# 每次备份做的事：
#   1. pg_dump 自定义格式写到 .partial → 把整份文件从头到尾解一遍（pg_restore 输出丢掉），
#      确认每一块数据都读得出来 → 改名为 .dump。
#   2. 管理后台里改过的配置（数据盘上的 bot/config，只读挂在 BACKUP_CONFIG_DIR）打成同名的 .config.tgz。
#      它不在数据库里，丢了的话后台改过的东西全部回到默认值。
#   3. 配了第二个存放处（BACKUP_COPY_DIR，应该在另一块盘上）就把这两个文件再拷一份过去，那边只留最新的几份。
#      数据库和备份在同一块盘上的话，盘坏了两样一起没。
#   4. 删除超过保留天数的旧文件。只有新备份成功后才清理旧的，所以连续失败时不会把旧备份删光。
#   5. 把结果记进数据库的 backup_runs 表，管理后台据此显示「最近一次备份」，一天多没有成功的就标红。
#      记不进去（库连不上、表还没建）不算备份失败——后台看的是「最近有没有成功的记录」，没有记录一样会标红。
#
# 恢复（restore）：先把备份恢复到一个新建的空库里，完整恢复成功之后才和正在用的库对调名字；
# 原来的库改名留着（<库名>_before_<时间>），确认没问题后再删。中途任何一步失败，正在用的库都没动过。
# 不用「往现有的库上覆盖」（pg_restore --clean）的做法：备份之后才建的表它不会删，迁移记录却回到了备份时的样子，
# 机器人启动时重跑迁移会撞上「表已存在」而起不来。
# 恢复前要先停掉连着数据库的服务：ctl stop qqbot kbsync
set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
BACKUP_TIME=${BACKUP_TIME:-03:30}
# 第二个存放处和那边留几份；没配、或者和 BACKUP_DIR 是同一个目录时不拷
COPY_DIR=${BACKUP_COPY_DIR:-}
COPY_KEEP=${BACKUP_COPY_KEEP:-3}
# 管理后台改过的配置所在的目录（只读挂载）；目录不存在就是后台还没改过任何东西
CONFIG_DIR=${BACKUP_CONFIG_DIR:-}
# 健康检查的阈值：比一天多留两小时余量
MAX_AGE_MINUTES=${BACKUP_MAX_AGE_MINUTES:-1560}
DB=${PGDATABASE:?缺少 PGDATABASE}

log() { printf '%s backup %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

has_recent() { # $1 = 分钟数
  [ -n "$(find "$BACKUP_DIR" -maxdepth 1 -name "${DB}-*.dump" -mmin "-$1" -print -quit)" ]
}

# 第二个存放处是不是真的另一个目录（没配、不存在、或者其实就是 BACKUP_DIR 时返回 1）
has_copy_dir() {
  [ -n "$COPY_DIR" ] && [ -d "$COPY_DIR" ] \
    && [ "$(stat -c %d:%i "$COPY_DIR")" != "$(stat -c %d:%i "$BACKUP_DIR")" ]
}

# 把一次备份的结果记进数据库。$1 = 成没成（true/false），$2 = 文件名，$3 = 字节数，
# $4 = 第二份拷了没有（true/false），$5 = 说明（哪一步出的问题；没有问题是空的）
record() {
  psql -X -q -v ON_ERROR_STOP=1 \
    -v ok="$1" -v file="$2" -v bytes="${3:-0}" -v copied="$4" -v detail="$5" >/dev/null 2>&1 <<'SQL' \
    || log "could not record the result in backup_runs"
INSERT INTO backup_runs (ok, file, bytes, copied, detail)
VALUES (:'ok'::boolean, :'file', :'bytes'::bigint, :'copied'::boolean, :'detail');
DELETE FROM backup_runs WHERE finished_at < now() - interval '90 days';
SQL
}

# 把文件 $1 拷到第二个存放处：先写 .partial 再改名，那边不会出现半个文件
copy_out() {
  local name
  name=$(basename "$1")
  cp "$1" "$COPY_DIR/$name.partial" && mv "$COPY_DIR/$name.partial" "$COPY_DIR/$name"
}

run_once() {
  local stamp final tmp size config="" copied=false problems="" kind
  stamp=$(date '+%Y%m%d-%H%M%S')
  final="$BACKUP_DIR/${DB}-$stamp.dump"
  tmp="$final.partial"
  umask 077
  log "start file=$final"
  if ! pg_dump --format=custom --compress=6 --file="$tmp"; then
    log "FAILED step=pg_dump"
    rm -f "$tmp"
    record false "" 0 false "pg_dump 没有成功"
    return 1
  fi
  # 只列目录（--list）查不出文件中间坏了；这里把整份解一遍，哪一块读不出来都会报错
  if ! pg_restore --file=/dev/null "$tmp"; then
    log "FAILED step=verify"
    rm -f "$tmp"
    record false "" 0 false "备份文件写出来了但读不回来"
    return 1
  fi
  mv "$tmp" "$final"
  size=$(stat -c %s "$final")
  log "ok file=$final bytes=$size"

  # 后台改过的配置。打不成（比如有文件这个用户读不了）不影响上面那份数据库备份，但要记下来
  if [ -n "$CONFIG_DIR" ] && [ -d "$CONFIG_DIR" ]; then
    config="$BACKUP_DIR/${DB}-$stamp.config.tgz"
    if tar -czf "$config.partial" -C "$(dirname "$CONFIG_DIR")" "$(basename "$CONFIG_DIR")" \
      && mv "$config.partial" "$config"; then
      log "ok config=$config bytes=$(stat -c %s "$config")"
    else
      log "FAILED step=config"
      rm -f "$config.partial"
      config=""
      problems="后台改过的配置没有备份成"
    fi
  fi

  if has_copy_dir; then
    if copy_out "$final" && { [ -z "$config" ] || copy_out "$config"; }; then
      copied=true
      log "ok copy_dir=$COPY_DIR"
      # 那边只留最新的几份（按文件名排，文件名里是时间）
      for kind in dump config.tgz; do
        find "$COPY_DIR" -maxdepth 1 -name "${DB}-*.$kind" | sort -r | tail -n "+$((COPY_KEEP + 1))" \
          | while read -r old; do rm -f "$old" && log "pruned copy=$old"; done
      done
    else
      log "FAILED step=copy dir=$COPY_DIR"
      rm -f "$COPY_DIR"/*.partial
      problems="${problems:+$problems；}第二份没有拷成"
    fi
  fi

  # 清理：过期的备份，以及以前中断留下的半成品
  find "$BACKUP_DIR" -maxdepth 1 \( -name "${DB}-*.dump" -o -name "${DB}-*.config.tgz" \) \
    -mmin "+$((KEEP_DAYS * 1440))" -print -delete \
    | while read -r old; do log "pruned file=$old"; done
  find "$BACKUP_DIR" -maxdepth 1 -name "${DB}-*.partial" -mmin +60 -delete

  record true "$(basename "$final")" "$size" "$copied" "$problems"
}

# 用备份 $1（文件名，在 BACKUP_DIR 或第二个存放处里）恢复数据库。做法见文件开头
restore() {
  local name=${1:?用法: backup.sh restore <文件名>} file="" fresh old others dir
  name=$(basename "$name")
  for dir in "$BACKUP_DIR" "$COPY_DIR"; do
    if [ -n "$dir" ] && [ -f "$dir/$name" ]; then
      file="$dir/$name"
      break
    fi
  done
  [ -n "$file" ] || { echo "找不到备份文件 ${name}（ls $BACKUP_DIR 看有哪些）" >&2; return 1; }
  pg_restore --file=/dev/null "$file" || { echo "这份备份读不出来，没有动数据库" >&2; return 1; }

  # 以下都连到 postgres 这个库上操作：改名、建库、删库都不能在被操作的库里做
  others=$(psql -X -At -d postgres -v db="$DB" <<'SQL'
SELECT count(*) FROM pg_stat_activity WHERE datname = :'db' AND pid <> pg_backend_pid();
SQL
  )
  if [ "$others" != 0 ]; then
    echo "还有 $others 个连接连着数据库 ${DB}。先停掉机器人和知识库更新：ctl stop qqbot kbsync" >&2
    return 1
  fi

  fresh="${DB}_restoring"
  old="${DB}_before_$(date '+%Y%m%d_%H%M%S')"
  dropdb --if-exists "$fresh" 2>/dev/null # 上次恢复到一半留下的；没有的话这句什么都不做
  createdb "$fresh"
  log "restore start file=$file into=$fresh"
  if ! pg_restore --no-owner --exit-on-error --dbname="$fresh" "$file"; then
    dropdb --if-exists "$fresh"
    echo "恢复到一半出错了（见上面的报错）。正在用的数据库 $DB 没有动过。" >&2
    return 1
  fi
  # 两次改名放在一个事务里：要么都成，要么都不成，不会出现「旧库改走了、新库还没顶上」
  if ! psql -X -q -d postgres -v ON_ERROR_STOP=1 -v db="$DB" -v fresh="$fresh" -v old="$old" <<'SQL'; then
BEGIN;
ALTER DATABASE :"db" RENAME TO :"old";
ALTER DATABASE :"fresh" RENAME TO :"db";
COMMIT;
SQL
    dropdb --if-exists "$fresh"
    echo "对调库名没有成功，正在用的数据库 $DB 没有动过。" >&2
    return 1
  fi
  log "restore ok file=$file previous_database=$old"
  echo "恢复完成。原来的数据库改名为 $old，留着没删。"
  echo "现在可以启动服务了（ctl start qqbot kbsync）。机器人启动时会把这份备份之后新增的表结构补上。"
  echo "确认没问题之后删掉原来的库：ctl exec -T backup dropdb $old"
  if [ -f "${file%.dump}.config.tgz" ]; then
    echo "同一时刻的后台配置在 ${file%.dump}.config.tgz 里，需要的话解到数据盘的 bot/ 目录下（见 README）。"
  fi
}

seconds_until_next() {
  local now target
  now=$(date +%s)
  target=$(date -d "today $BACKUP_TIME" +%s)
  if [ "$target" -le "$now" ]; then
    target=$(date -d "tomorrow $BACKUP_TIME" +%s)
  fi
  echo $((target - now))
}

nap() { # 可被 SIGTERM 打断的 sleep
  sleep "$1" &
  wait $!
}

loop() {
  trap 'log "stopping"; exit 0' TERM INT
  log "started time=$BACKUP_TIME keep_days=$KEEP_DAYS dir=$BACKUP_DIR" \
    "copy_dir=$(has_copy_dir && echo "$COPY_DIR" || echo none)"
  # 刚部署或停机很久之后：没有 24 小时内的备份就先补一份
  if ! has_recent 1440; then
    run_once || log "initial backup failed, will retry at the scheduled time"
  fi
  while true; do
    wait_s=$(seconds_until_next)
    log "next run in ${wait_s}s"
    nap "$wait_s"
    if ! run_once; then
      log "retrying in 10 minutes"
      nap 600
      run_once || log "retry failed, giving up until tomorrow"
    fi
  done
}

case "${1:-loop}" in
  loop) loop ;;
  once) run_once ;;
  check) has_recent "$MAX_AGE_MINUTES" ;;
  restore) shift; restore "$@" ;;
  *)
    echo "用法: $0 [loop|once|check|restore <文件名>]" >&2
    exit 2
    ;;
esac
