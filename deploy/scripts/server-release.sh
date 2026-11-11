#!/usr/bin/env bash
# 发布逻辑里在服务器上执行的那一半：切换（先检查、互斥、起不来自动切回）和清理旧发布。
#
# 不单独运行。deploy/scripts/release.sh 经 ssh 把这个文件从标准输入喂给服务器上的 bash：
#   ssh <服务器> bash -s -- activate <根目录> <发布号> <保留几个> < server-release.sh
#   ssh <服务器> bash -s -- prune    <根目录> <保留几个>          < server-release.sh
# 所以用的总是开发机上的这一版，和要切到哪个发布无关——回滚到还没有这个文件的老发布也是同一套做法。
#
# 整个文件包在 main 里、最后一行才调用：bash 是边读边执行标准输入的，这样写它会先把整个文件读完，
# 中途的命令（docker compose 之类）读不到也改不了后面的脚本。
set -euo pipefail

main() {
  local action=${1:?缺少动作}
  ROOT=${2:?缺少根目录}
  shift 2
  WAIT_SECONDS=${JIANSHANAGENT_WAIT_SECONDS:-180}

  # 互斥：同一时间只让一个发布操作动 current 和镜像。拿不到锁直接退出，不排队
  # （排队的话，后一个会在前一个刚切完时又切一次，两边都以为是自己的版本在线上）。
  exec 9>"$ROOT/.release.lock"
  flock -n 9 || die "服务器上有另一个发布操作正在进行，等它结束再来"

  case "$action" in
    activate) activate "$@" ;;
    prune) prune "$@" ;;
    *) die "不认识的动作：$action" ;;
  esac
}

die() {
  echo "$*" >&2
  exit 1
}

# 发布号 = 提交的 7 位短号，或者「提交短号-目录树短号」（叠加了工作区部署文件的发布）。
# 别的样子一律不认：它会被拼进路径和镜像名，也免得清理时删到不是发布的东西。
valid_release() { [[ $1 =~ ^[0-9a-f]{7}(-[0-9a-f]{7})?$ ]]; }

# 软链 $1 指向的发布号；软链不存在时什么都不输出
linked_release() {
  local target
  target=$(readlink "$ROOT/$1" 2>/dev/null) || return 0
  basename "$target"
}

# 把软链 $1 换成指向发布 $2。先建一个新的再改名盖上去：改名是一步完成的，
# 任何时刻去读这个软链，读到的不是旧的就是新的，不会有「不存在」的一瞬间
point() {
  ln -sfnT "releases/$2" "$ROOT/$1.new"
  mv -T "$ROOT/$1.new" "$ROOT/$1"
}

note() { # 记一笔切换，以后查「什么时候从哪个版本换到哪个版本、成没成」
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$ROOT/activations.log"
}

ctl() { (cd "$ROOT" && ./ctl "$@") </dev/null; }

# 用 current 指向的发布起栈。--wait：等有健康检查的服务都变成 healthy 才算成功
bring_up() { ctl up -d --remove-orphans --wait --wait-timeout "$WAIT_SECONDS"; }

# 切换之前，先让现在这个版本的机器人和知识库更新停稳（用的还是旧发布的 compose 文件）。
# 不先停的话，up 会按依赖顺序挨个重建容器：数据库的定义有变化时它先被换掉，旧机器人还在答的消息就写不进库；
# 日志采集（部署插件 logs 的 Alloy）每次发布都要重建，旧机器人退出前那几行日志——等了几条在途消息、有没有答完——也来不及采走。
# 机器人收到停止信号后会等在途的消息答完（最多 20 秒，见 config/gateway.qq.yaml 的 shutdown），所以这一步最长半分钟。
quiesce() {
  ctl stop qqbot || true
  ctl stop kbsync 2>/dev/null || true # 老发布里没有这个服务
  sleep "${JIANSHANAGENT_LOG_FLUSH_SECONDS:-3}" # 等日志采集把最后几行日志送走
}

# 不切换，只让 compose 把发布 $1 的文件连同 shared/.env 解析一遍：少了变量、文件写错了、
# 列了那个发布里没有的部署插件，在这里就报出来。用的是那个发布自己的 ctl——带哪些文件由它说了算
check_compose() {
  JIANSHANAGENT_ROOT=$ROOT CTL_RELEASE=$1 "$ROOT/releases/$1/deploy/scripts/ctl" config -q </dev/null
}

# 发布 $1 里启用的部署插件各自的准备（建数据目录、补 .env 里缺的变量）：插件目录里有 init.sh 就跑一遍。
# 这些脚本只添不改、可以重复执行，所以放在检查之前——检查要用到它们补上的变量。
# 老发布没有部署插件（deploy/plugins/），什么都不做；.env 里没列的、列错的，留给后面的检查去报。
prepare_plugins() {
  local dir="$ROOT/releases/$1/deploy/plugins" env_file="$ROOT/shared/.env" data id
  [ -d "$dir" ] && [ -f "$env_file" ] || return 0
  data=$(sed -n 's/^JIANSHANAGENT_DATA_DIR=//p' "$env_file" | tail -n 1)
  for id in $(sed -n 's/^JIANSHANAGENT_DEPLOY_PLUGINS=//p' "$env_file" | tail -n 1 | tr -d "\"'" | tr ',' ' '); do
    [[ $id =~ ^[a-z0-9_-]+$ ]] && [ -f "$dir/$id/init.sh" ] || continue
    JIANSHANAGENT_DATA_DIR=$data JIANSHANAGENT_ENV_FILE=$env_file bash "$dir/$id/init.sh" </dev/null \
      || die "部署插件「$id」的准备（$dir/$id/init.sh）没有成功，没有切换，线上没动"
  done
}

activate() {
  local rel=${1:?缺少发布号} keep=${2:-0} previous
  valid_release "$rel" || die "发布号的样子不对：$rel"
  [ -f "$ROOT/releases/$rel/deploy/docker-compose.yml" ] || die "服务器上没有发布 $rel"
  docker image inspect "jianshanagent-qqbot:$rel" >/dev/null 2>&1 \
    || die "镜像 jianshanagent-qqbot:$rel 还没构建，先 release.sh push"
  prepare_plugins "$rel"
  check_compose "$rel" || die "发布 $rel 的 Compose 文件过不了检查（见上面的报错），没有切换，线上没动"

  previous=$(linked_release current)
  if [ -n "$previous" ] && [ "$previous" != "$rel" ]; then
    quiesce
  fi
  point current "$rel"
  if bring_up; then
    # 上一个发布记下来，回滚时不用去翻是哪个；重新激活同一个发布时不覆盖原来记的
    if [ -n "$previous" ] && [ "$previous" != "$rel" ]; then
      point previous "$previous"
    fi
    note "from=${previous:-none} to=$rel result=ok"
    ctl ps || true
    # 清理放在子 shell 里：它出了岔子只报一句，不能让一次已经成功的上线看起来像失败
    (prune "$keep") || echo "清理旧发布时出了问题（不影响这次上线），可以之后再 release.sh prune" >&2
    return 0
  fi

  echo "新发布 $rel 没有在 ${WAIT_SECONDS} 秒内起来。各服务的状态和机器人最后的日志：" >&2
  ctl ps -a >&2 || true
  ctl logs --tail 40 qqbot >&2 || true
  if [ -z "$previous" ] || [ "$previous" = "$rel" ] || [ ! -d "$ROOT/releases/$previous" ]; then
    note "from=${previous:-none} to=$rel result=failed"
    die "没有可以切回去的上一个发布，current 仍指向 ${rel}，需要人来处理"
  fi
  echo "切回上一个发布 ${previous}……" >&2
  point current "$previous"
  if bring_up; then
    note "from=$previous to=$rel result=rolled_back"
    ctl ps >&2
    die "已切回 ${previous}，它运行正常。发布 $rel 没有上线（目录和镜像都留着，查明原因后可以再试）"
  fi
  note "from=$previous to=$rel result=rollback_failed"
  ctl ps -a >&2 || true
  die "切回 $previous 之后也没有起来，current 现在指向 ${previous}，需要人来处理"
}

# 清理旧发布：留着 current、previous 指向的和最新上传的 $1 个，其余的连同它们的镜像删掉。
# $1 为 0 时什么都不做。只碰 releases/ 下名字像发布号的目录和 jianshanagent-qqbot 这一个镜像仓库的标签，
# 别的项目的镜像、构建缓存都不动。
prune() {
  local keep=${1:-0} current previous rel seen=0
  [[ $keep =~ ^[0-9]+$ ]] || die "保留几个应该是数字：$keep"
  [ "$keep" -gt 0 ] || return 0
  current=$(linked_release current)
  previous=$(linked_release previous)
  [ -n "$current" ] || die "current 还没指向任何发布，不清理"

  # 按上传时间从新到旧（发布目录传完就不再改，目录的修改时间就是上传完成的时间）
  while read -r rel; do
    valid_release "$rel" || continue
    seen=$((seen + 1))
    if [ "$seen" -le "$keep" ] || [ "$rel" = "$current" ] || [ "$rel" = "$previous" ]; then
      continue
    fi
    remove_image "$rel" || continue
    rm -rf -- "${ROOT:?}/releases/$rel"
    echo "清掉旧发布 $rel"
  done < <(ls -1t "$ROOT/releases")

  # 发布目录已经不在了的镜像（以前手动删过目录、或者上传到一半失败的）
  while read -r rel; do
    valid_release "$rel" || continue
    [ -d "$ROOT/releases/$rel" ] && continue
    remove_image "$rel" && echo "清掉没有发布目录的镜像 jianshanagent-qqbot:$rel"
  done < <(docker images jianshanagent-qqbot --format '{{.Tag}}')
  return 0
}

# 删掉发布 $1 的镜像标签。不带 -f：有容器（哪怕是停着的）还在用它时 docker 会拒绝，那就留着
remove_image() {
  docker image inspect "jianshanagent-qqbot:$1" >/dev/null 2>&1 || return 0
  if ! docker image rm "jianshanagent-qqbot:$1" >/dev/null 2>&1; then
    echo "镜像 jianshanagent-qqbot:$1 还有容器在用，这个发布先留着" >&2
    return 1
  fi
}

main "$@"
