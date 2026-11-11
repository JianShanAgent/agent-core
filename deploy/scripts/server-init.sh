#!/usr/bin/env bash
# 服务器一次性初始化（在服务器上以 root 运行，可以重复执行，不会覆盖已有的密码和数据）：
#   server-init.sh <数据目录> [对外绑定地址] [部署插件]
#   例：server-init.sh /home/jianshanagent <内网地址>
#   对外绑定地址：管理后台（和部署插件里要给人打开的页面）绑在哪个地址上，局域网访问就填服务器的内网地址；不填只绑本机。
#   部署插件：要带哪些（deploy/plugins/ 下的目录名，逗号隔开），不填是 logs（运行日志）；一个都不带写 ""。
#
# 做的事：
#   1. 确认数据目录所在的文件系统支持 Unix 权限（exFAT / NTFS / FAT 上不能放 Postgres，直接拒绝）
#   2. 建 /opt/jianshanagent/{releases,shared} 和数据目录下各服务的子目录，属主设成容器里的运行用户
#   3. 没有 shared/.env 时生成一份，数据库密码随机生成（不打印）
# 部署插件自己要的目录和变量不在这里管：每次上线前由插件目录里的 init.sh 补上（见 server-release.sh）。
set -euo pipefail

ROOT=${JIANSHANAGENT_ROOT:-/opt/jianshanagent}
DATA=${1:?用法: server-init.sh <数据目录> [对外绑定地址] [部署插件]}
BIND_ADDR=${2:-127.0.0.1}
DEPLOY_PLUGINS=${3-logs}

parent=$(dirname "$DATA")
fstype=$(stat -f -c %T "$parent")
case "$fstype" in
  xfs | ext2/ext3 | ext4 | btrfs | zfs) ;;
  *)
    echo "数据目录所在文件系统是 ${fstype}，不在已确认可用的列表里（xfs / ext4 / btrfs / zfs）。" >&2
    echo "exFAT、NTFS、FAT 不支持 Unix 权限，Postgres 不能放在上面。已停止，未做任何改动。" >&2
    exit 1
    ;;
esac
echo "数据目录：${DATA}（文件系统 ${fstype}）"

install -d -m 755 "$ROOT" "$ROOT/releases"
install -d -m 700 "$ROOT/shared"
ln -sfn current/deploy/scripts/ctl "$ROOT/ctl"

# 属主对应各镜像里的运行用户：postgres 999、机器人 10001
install -d -m 755 "$DATA"
install -d -m 700 -o 999 -g 999 "$DATA/postgres" "$DATA/backups"
install -d -m 755 -o 10001 -g 10001 "$DATA/bot"
install -d -m 755 "$DATA/redis"

env_file="$ROOT/shared/.env"
if [ -e "$env_file" ]; then
  echo "已有 ${env_file}，保持不动"
else
  umask 077
  cat >"$env_file" <<EOF
# 由 server-init.sh 生成于 $(date '+%Y-%m-%d %H:%M:%S')。各项含义见 deploy/.env.example
POSTGRES_PASSWORD=$(openssl rand -hex 24)
JIANSHANAGENT_DATA_DIR=$DATA
JIANSHANAGENT_SECRETS_FILE=$ROOT/shared/secrets.yaml
# 带哪些部署插件（deploy/plugins/ 下的目录名，逗号隔开；一个都不带就留空，但这一行要在）
JIANSHANAGENT_DEPLOY_PLUGINS=$DEPLOY_PLUGINS
HEALTH_PORT=18080
ADMIN_BIND_ADDR=$BIND_ADDR
ADMIN_PORT=13001
BACKUP_TIME=03:30
BACKUP_KEEP_DAYS=14
# 备份的第二个存放处：填另一块盘上的目录（事先建好，属主 999），每次备份完再拷一份过去。见 README「备份和恢复」
# BACKUP_COPY_DIR=
EOF
  chmod 600 "$env_file"
  echo "已生成 ${env_file}（密码随机，未打印；部署插件：${DEPLOY_PLUGINS:-不带}）"
fi

if [ ! -e "$ROOT/shared/secrets.yaml" ]; then
  echo "还缺 $ROOT/shared/secrets.yaml：在开发机上运行 deploy/scripts/release.sh secrets 上传"
fi
