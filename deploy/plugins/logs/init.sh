#!/usr/bin/env bash
# 部署插件「运行日志」上线前的准备。scripts/server-release.sh 在切换之前调用（以 root），可以重复执行：
# 已经有的目录、已经写了的变量都不动。
#   1. 数据目录下建 loki/ grafana/ alloy/，属主是各镜像里的运行用户（loki 10001、grafana 472）；
#   2. shared/.env 里没有 Grafana 管理员密码就随机生成一个（不打印）；
#      没写 Grafana 绑在哪个地址，就和管理后台绑同一个（ADMIN_BIND_ADDR），局域网里能直接打开。
# 调用的一方给两个环境变量：JIANSHANAGENT_DATA_DIR（数据目录）、JIANSHANAGENT_ENV_FILE（shared/.env 的位置）。
set -euo pipefail

DATA=${JIANSHANAGENT_DATA_DIR:?缺少 JIANSHANAGENT_DATA_DIR}
ENV_FILE=${JIANSHANAGENT_ENV_FILE:?缺少 JIANSHANAGENT_ENV_FILE}

[ -d "$DATA/loki" ] || install -d -m 755 -o 10001 -g 10001 "$DATA/loki"
[ -d "$DATA/grafana" ] || install -d -m 755 -o 472 -g 0 "$DATA/grafana"
[ -d "$DATA/alloy" ] || install -d -m 755 "$DATA/alloy"

has() { grep -q "^$1=" "$ENV_FILE"; }
add() { # 往 .env 末尾加一行；原来最后一行没有换行符的话先补上
  [ -z "$(tail -c 1 "$ENV_FILE")" ] || echo >>"$ENV_FILE"
  printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
}

if ! has GRAFANA_ADMIN_PASSWORD; then
  add GRAFANA_ADMIN_PASSWORD "$(openssl rand -hex 16)"
  echo "运行日志：已在 $ENV_FILE 里生成 Grafana 管理员密码（未打印）"
fi
if ! has GRAFANA_BIND_ADDR; then
  addr=$(sed -n 's/^ADMIN_BIND_ADDR=//p' "$ENV_FILE" | tail -n 1)
  if [ -n "$addr" ]; then
    add GRAFANA_BIND_ADDR "$addr"
    echo "运行日志：Grafana 和管理后台绑同一个地址（${addr}，端口 GRAFANA_PORT，默认 13000）"
  fi
fi
