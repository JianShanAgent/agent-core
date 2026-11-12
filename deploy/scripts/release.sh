#!/usr/bin/env bash
# 在开发机上、在**实例仓库**（instance-*）的目录里运行：把一个部署发布到服务器。
# 服务器不从 GitHub 拉代码，代码由这里打包传过去。
#
#   release.sh deploy [提交]         一键上传当前提交、构建镜像并激活上线（起不来自动切回上一个）
#   release.sh pin                   把旁边各个仓库现在的提交记进 components.lock（然后提交实例仓库）
#   release.sh push [提交]           打包上传到 releases/<发布号>/ 并构建机器人镜像（不影响正在运行的服务）
#   release.sh activate [发布号]     切到该发布（不写发布号默认切当前提交）：先检查，起不来自动切回原来的
#   release.sh rollback              切回上一个发布（服务器上记着是哪个）
#   release.sh prune [留几个]        清理旧发布的目录和镜像（activate 成功后会自动做，默认留 5 个）
#   release.sh secrets               上传 config/secrets.yaml 到 shared/（权限 600）
#   release.sh list                  列出服务器上的发布、当前和上一个指向、最近几次切换
#   release.sh kb-seed [wiki]        把开发机的知识库文件（data/kb）拷到服务器，自动更新在它上面接着做
#                                    （写了 wiki 的 id 就只拷那一个 wiki 的）
#   release.sh webui [提交]          只在本机构建管理后台的页面，看能不能构建（不上传、不碰服务器）
#
# 一个部署由几个仓库拼成：核心（jianshanagent，也带着部署工具 deploy/）、用到的插件、实例仓库自己
# （这个部署的配置 config/）。用哪几个仓库的哪个提交写在实例仓库的 components.lock 里：
#
#     # 名字          开发机上的目录（相对实例仓库）   提交
#     core            ../agent-core                3f2a…
#     site-huiji-yys  ../site-huiji-yys            91bc…
#
# 第一行必须是核心。发布号 = 实例仓库那个提交的 7 位短号——它连同 components.lock 一起，把整个部署
# 用的代码都定死了。上传的内容只来自各仓库 git 里的那些提交，工作区里没提交的改动不会带上去。
#
# 服务器上一个发布的样子：
#     releases/<发布号>/
#       RELEASE            发布号、各个仓库的提交
#       config/            实例仓库的 config/（这个部署和默认值不一样的地方）
#       deploy/            核心的 deploy/（compose、脚本、部署插件）
#       components/<名字>/  核心和各个插件的源码（pyproject.toml、src/），镜像从这里装
#
# 管理后台的页面（核心的 webui/）不是源码直接能用的，要先构建成静态文件。push 时在开发机上用核心那个
# 提交里的 webui/ 源码构建（在临时目录里做，不是用工作区的），产物随核心的源码一起传上去。
# 所以开发机上要有 Node（npm）；服务器上不需要。
set -euo pipefail

# 发布到哪台服务器：ssh 认得的名字或地址。由实例仓库的 ./release 给出
HOST=${JIANSHANAGENT_SSH_HOST:?没有指定服务器：在实例仓库的 release 里填 SSH_HOST，或者设环境变量 JIANSHANAGENT_SSH_HOST}
ROOT=${JIANSHANAGENT_ROOT:-/opt/jianshanagent}
LOCK=components.lock
# 这个脚本所在的目录（后面要换目录，先记下绝对路径）
SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

cd "$(git rev-parse --show-toplevel)"
[ -f "$LOCK" ] || {
  echo "这里没有 ${LOCK}：release.sh 要在实例仓库（instance-*）的目录里运行" >&2
  exit 1
}

remote() { ssh -o BatchMode=yes "$HOST" "$@"; }

# 在服务器上执行 server-release.sh 里的一个动作。脚本从标准输入传过去，用的是开发机上的这一版
# （和要切到哪个发布无关）。参数只有根目录、发布号和数字，发布号事先校验过，这里再统一加一层引号。
remote_script() {
  local quoted
  quoted=$(printf ' %q' "$@")
  ssh -o BatchMode=yes "$HOST" "bash -s --$quoted" <"$SCRIPTS/server-release.sh"
}

# 和 server-release.sh 里的同名函数是同一个规则
valid_release() { [[ $1 =~ ^[0-9a-f]{7}(-[0-9a-f]{7})?$ ]]; }

# 每次切换成功后清理旧发布：除了正在用的和上一个，再留最新的这么多个（目录和镜像一起删）
KEEP_RELEASES=${JIANSHANAGENT_KEEP_RELEASES:-5}

# 管理后台页面构建出来放在核心仓库里的哪个位置（和 webui/vite.config.ts 的 outDir 一致）
WEBUI_STATIC=src/jianshanagent/admin/static

# 读 components.lock 的内容（从标准输入），每个仓库输出一行「名字<TAB>目录<TAB>提交」；格式不对就报错
components() {
  local name dir commit extra first=yes
  while read -r name dir commit extra; do
    case $name in '' | '#'*) continue ;; esac
    [[ $name =~ ^[a-z0-9][a-z0-9-]*$ ]] && [ -n "$dir" ] && [ -z "${extra:-}" ] \
      && [[ ${commit:-} =~ ^[0-9a-f]{40}$ ]] \
      || { echo "$LOCK 里这一行不对：$name $dir ${commit:-} ${extra:-}（要写「名字 目录 40 位的提交」）" >&2; return 1; }
    if [ "$first" = yes ] && [ "$name" != core ]; then
      echo "$LOCK 的第一行要是核心（名字写 core）" >&2
      return 1
    fi
    first=no
    printf '%s\t%s\t%s\n' "$name" "$dir" "$commit"
  done
  [ "$first" = no ] || { echo "$LOCK 里一个仓库都没有" >&2; return 1; }
}

# 仓库 $1（目录）里有没有提交 $2
has_commit() { git -C "$1" cat-file -e "$2^{commit}" 2>/dev/null; }

# 用核心（目录 $1）提交 $2 里的 webui/ 源码，在临时目录 $3 里构建管理后台的页面；产物在 $3/$WEBUI_STATIC。
# 那个提交里没有 webui/ 时返回 1，什么都不做。构建的输出走标准错误。
build_webui() {
  local core=$1 commit=$2 work=$3
  git -C "$core" cat-file -e "$commit:webui/package.json" 2>/dev/null || return 1
  command -v npm >/dev/null \
    || { echo "核心带管理后台的页面，构建要用 Node（npm），这台机器上没找到" >&2; exit 1; }
  # 这个函数是放在 if 里调用的，出错不会自动停下来，所以每一步都自己判断
  git -C "$core" archive --format=tar "$commit" webui | tar -x -C "$work" \
    || { echo "取不出核心那个提交里的 webui/" >&2; exit 1; }
  # 依赖：提交里的 package-lock.json 和核心工作区的一样、工作区又装过依赖，就直接用工作区那份；
  # 否则按锁文件现装一份（要联网）
  if [ -d "$core/webui/node_modules" ] && git -C "$core" diff --quiet "$commit" -- webui/package-lock.json; then
    ln -s "$(cd "$core" && pwd)/webui/node_modules" "$work/webui/node_modules"
  else
    (cd "$work/webui" && npm ci --no-audit --no-fund) >&2 \
      || { echo "管理后台页面的依赖没装上（npm ci）" >&2; exit 1; }
  fi
  (cd "$work/webui" && npm run build) >&2 \
    || { echo "管理后台的页面没构建成功，没有上传任何东西" >&2; exit 1; }
  [ -f "$work/$WEBUI_STATIC/index.html" ] \
    || { echo "管理后台的页面构建完没有产物（$WEBUI_STATIC/index.html）" >&2; exit 1; }
}

# 实例仓库提交 $1 的 components.lock 里，核心那一行：输出「目录<TAB>提交」
core_of() {
  git show "$1:$LOCK" | components | awk -F'\t' 'NR == 1 { print $2 "\t" $3 }'
}

cmd_webui() {
  local commit core core_commit work
  commit=$(git rev-parse --verify "${1:-HEAD}^{commit}")
  IFS=$'\t' read -r core core_commit < <(core_of "$commit")
  has_commit "$core" "$core_commit" || { echo "$core 里没有提交 $core_commit（先在那里 git fetch）" >&2; exit 1; }
  work=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$work'" EXIT
  if build_webui "$core" "$core_commit" "$work"; then
    echo "核心 ${core_commit:0:7} 的管理后台页面可以构建："
    (cd "$work/$WEBUI_STATIC" && find . -type f | wc -l | tr -d ' ' | sed 's/$/ 个文件/' && du -sh . | cut -f1)
  else
    echo "核心 ${core_commit:0:7} 里没有 webui/，不用构建"
  fi
}

# 把旁边各个仓库现在的提交写进 components.lock。目录照现有的；仓库里有没提交的改动时提醒一句
#（发布只带提交了的内容）。写完要自己提交实例仓库——发布号就是那个提交。
cmd_pin() {
  local name dir commit head tmp
  tmp=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" EXIT
  sed -n '/^#/p' "$LOCK" >"$tmp"
  while IFS=$'\t' read -r name dir commit; do
    head=$(git -C "$dir" rev-parse --verify 'HEAD^{commit}') \
      || { echo "$dir 不是一个 git 仓库（$name）" >&2; exit 1; }
    [ -z "$(git -C "$dir" status --porcelain)" ] \
      || echo "注意：$dir 里有没提交的改动，发布不会带上它们" >&2
    printf '%-16s %-28s %s\n' "$name" "$dir" "$head" >>"$tmp"
    [ "$head" = "$commit" ] || echo "$name: ${commit:0:7} → ${head:0:7}"
  done < <(components <"$LOCK")
  if cmp -s "$tmp" "$LOCK"; then
    echo "$LOCK 没有变化"
  else
    cat "$tmp" >"$LOCK"
    echo "已更新 ${LOCK}，提交实例仓库之后再 release.sh push"
  fi
}

cmd_push() {
  local commit rel work webui=none name dir sha core="" core_commit="" list
  commit=$(git rev-parse --verify "${1:-HEAD}^{commit}")
  rel=$(git rev-parse --short=7 "$commit")
  list=$(git show "$commit:$LOCK" | components)
  while IFS=$'\t' read -r name dir sha; do
    has_commit "$dir" "$sha" \
      || { echo "$dir 里没有 $name 要用的提交 $sha（先在那里 git fetch，或者 release.sh pin）" >&2; exit 1; }
    [ -n "$core" ] || { core=$dir; core_commit=$sha; }
  done <<<"$list"

  echo "发布号 ${rel}（实例仓库提交 ${commit}）"
  echo "$list" | awk -F'\t' '{ printf "  %-16s %s\n", $1, $3 }'
  if remote "test -e '$ROOT/releases/$rel'"; then
    echo "服务器上已有 $ROOT/releases/${rel}，不重复上传；直接构建镜像"
  else
    # 先在本机把管理后台的页面构建好：构建不过就不上传，服务器上不会留下半个发布
    work=$(mktemp -d)
    # shellcheck disable=SC2064
    trap "rm -rf '$work'" EXIT
    if build_webui "$core" "$core_commit" "$work"; then
      webui=built
    fi
    local up="$ROOT/releases/$rel.uploading"
    remote "rm -rf '$up' && mkdir -p '$up/components'"
    # 这个部署的配置（以及实例仓库自带的 sites/ 和 plugins/ 目录）
    local inst_paths=(config "$LOCK")
    for extra in sites plugins; do
      git cat-file -e "$commit:$extra" 2>/dev/null && inst_paths+=("$extra")
    done
    git -c tar.umask=0022 archive --format=tar "$commit" "${inst_paths[@]}" | remote "tar -x -C '$up'"
    remote "mkdir -p '$up/sites' '$up/plugins'"
    # 部署工具：核心的 deploy/
    git -C "$core" -c tar.umask=0022 archive --format=tar "$core_commit" deploy | remote "tar -x -C '$up'"
    # 核心和各个插件的源码
    # （循环里的 ssh 不能去读循环自己的输入，不然读完第一行后面的仓库就都漏了：没有管道喂它的那一句接 /dev/null）
    while IFS=$'\t' read -r name dir sha; do
      remote "mkdir -p '$up/components/$name'" </dev/null
      git -C "$dir" -c tar.umask=0022 archive --format=tar "$sha" \
        $(git -C "$dir" ls-tree --name-only "$sha" | grep -E '^(pyproject\.toml|uv\.lock|README\.md|src)$') \
        | remote "tar -x -C '$up/components/$name'"
    done <<<"$list"
    # 传完核对一遍：components.lock 里的每个仓库都到了
    remote "for name in $(echo "$list" | cut -f1 | tr '\n' ' '); do \
        test -f '$up/components/'\$name/pyproject.toml || { echo \"没传上去：\$name\" >&2; exit 1; }; done" \
      || { echo "上传不完整，没有构建镜像（服务器上的 $up 留着待查）" >&2; exit 1; }
    if [ "$webui" = built ]; then
      # COPYFILE_DISABLE：不把 macOS 的扩展属性打成 ._ 开头的文件带过去
      COPYFILE_DISABLE=1 tar -C "$work/$WEBUI_STATIC" -cf - . \
        | remote "mkdir -p '$up/components/core/$WEBUI_STATIC' \
          && tar -x --no-same-owner -C '$up/components/core/$WEBUI_STATIC'"
    fi
    {
      echo "release=$rel"
      echo "instance_commit=$commit"
      echo "$list" | awk -F'\t' '{ print "component." $1 "=" $3 }'
      echo "webui=$webui"
      echo "pushed_at=$(date '+%Y-%m-%dT%H:%M:%S%z')"
    } | remote "cat > '$up/RELEASE'"
    remote "mv '$up' '$ROOT/releases/$rel'"
  fi

  # 直接 docker build（不经 compose），这样第一次部署、shared/.env 还没生成时也能构建
  remote "docker build -f '$ROOT/releases/$rel/deploy/Dockerfile' -t 'jianshanagent-qqbot:$rel' \
    ${PIP_INDEX_URL:+--build-arg PIP_INDEX_URL='$PIP_INDEX_URL'} '$ROOT/releases/$rel'"
  echo "镜像 jianshanagent-qqbot:$rel 已构建。上线：release.sh activate $rel"
}

# 切换的全过程在服务器上一口气做完（server-release.sh 的 activate）：
#   1. 拿锁——同一时间只有一个发布操作；
#   2. 切换之前先检查：发布目录和镜像在不在、新的 Compose 文件连同 shared/.env 能不能解析。过不了就停，线上不动；
#   3. 用旧发布让机器人和知识库更新先停稳（机器人会等在途的消息答完），再把 current 指过去，
#      up -d --wait，等有健康检查的服务都健康；
#   4. 起不来：打印各服务状态和机器人最后的日志，把 current 指回原来的发布再 up 一次，然后报错退出；
#   5. 起来了：把原来的发布记成 previous（release.sh rollback 用），记一笔 activations.log，清理旧发布。
# 回切救不了的一种情况：新版本已经把数据库的结构改了（迁移在机器人启动时执行）。现有的迁移都只是加表加列，
# 旧代码不受影响；以后要是有删改列的迁移，回滚前先看一眼。
cmd_activate() {
  local rel=${1:-$(git rev-parse --short=7 'HEAD^{commit}')}
  valid_release "$rel" || { echo "发布号的样子不对：${rel}（应该是 7 位提交短号，或不传参数默认激活当前提交）" >&2; exit 1; }
  remote_script activate "$ROOT" "$rel" "$KEEP_RELEASES"
}

cmd_deploy() {
  local commit rel
  commit=$(git rev-parse --verify "${1:-HEAD}^{commit}")
  rel=$(git rev-parse --short=7 "$commit")
  cmd_push "$commit"
  cmd_activate "$rel"
}

cmd_rollback() {
  local previous
  previous=$(remote "basename \"\$(readlink '$ROOT/previous' 2>/dev/null)\" 2>/dev/null" || true)
  [ -n "$previous" ] || { echo "服务器上没有记着上一个发布（previous），用 release.sh list 看有哪些，再 activate <发布号>" >&2; exit 1; }
  echo "切回上一个发布 $previous"
  cmd_activate "$previous"
}

cmd_prune() {
  local keep=${1:-$KEEP_RELEASES}
  [[ $keep =~ ^[1-9][0-9]*$ ]] || { echo "用法: release.sh prune [保留最新的几个，至少 1]" >&2; exit 1; }
  remote_script prune "$ROOT" "$keep"
  cmd_list
}

cmd_secrets() {
  local src=config/secrets.yaml
  [ -f "$src" ] || { echo "找不到 $src" >&2; exit 1; }
  # 先以 600 完整地落到旁边的临时文件（内容不经过任何输出），再写进正式的文件。
  # 已有文件时是把内容写进去而不是改名替换：容器挂的是这一个文件，改名会换成另一个文件，
  # 容器里看到的还是旧的那个，要删掉容器重建才读得到；原地写的话，重启机器人就能读到。
  remote "umask 077 && cat > '$ROOT/shared/secrets.yaml.new' \
    && chown 10001:10001 '$ROOT/shared/secrets.yaml.new' \
    && chmod 600 '$ROOT/shared/secrets.yaml.new' \
    && if [ -f '$ROOT/shared/secrets.yaml' ]; then \
         cat '$ROOT/shared/secrets.yaml.new' > '$ROOT/shared/secrets.yaml' \
           && rm -f '$ROOT/shared/secrets.yaml.new'; \
       else \
         mv '$ROOT/shared/secrets.yaml.new' '$ROOT/shared/secrets.yaml'; \
       fi" <"$src"
  echo "已上传到 $HOST:$ROOT/shared/secrets.yaml（属主 10001，权限 600）。"
  echo "机器人重启后读到新凭据：管理后台「系统」页的重启，或者服务器上 $ROOT/ctl restart qqbot。"
}

cmd_list() {
  remote "ls -1t '$ROOT/releases' 2>/dev/null
    echo \"current  -> \$(readlink '$ROOT/current' 2>/dev/null || echo 无)\"
    echo \"previous -> \$(readlink '$ROOT/previous' 2>/dev/null || echo 没有记录)\"
    if [ -f '$ROOT/activations.log' ]; then echo '最近几次切换：'; tail -n 5 '$ROOT/activations.log'; fi"
}

# 把开发机的 data/kb 拷到服务器的数据盘上（容器里的 /data/kb）。
# data/kb 下面一个 wiki 一层（data/kb/<wiki 的 id>/）；写了 wiki 的 id 就只拷那一层，别的 wiki 在服务器上的文件不动。
#
# 服务器上的 kbsync 服务只做增量：它要有切片、向量和取数缓存（页面清单、各页面取下来的原始内容）
# 才知道哪些已经有了；数据盘上没有这些文件时它什么都不做，不会自己从头把整个 wiki 取一遍。
# 所以第一次上线自动更新之前、以后每新挂一个 wiki 都要拷一次；别的时候一般不用再拷（服务器自己会跟着 wiki 更新）。
# 再拷一次也没有坏处：服务器上的文件会回到开发机的那一版，下一轮自动更新再从那里追上来。
#
# 拷的是：切片和文档清单、图片记录、向量、取数缓存、自动更新读到哪里了（cache/sync_state.json）。
# 不拷的是：原图缓存（1 个 G；已经有向量的图片用不着原图，新图服务器会自己下）、评测和调试的产物、日志。
# 用 rsync -a：文件的修改时间要原样带过去——老的渲染缓存是拿它和页面的 touched 比来判断过没过时的。
cmd_kb_seed() {
  local wiki=${1:-} src=data/kb data dest dir found=0
  case "$wiki" in
    *[!a-z0-9_]*) echo "wiki 的 id 只有小写字母、数字、下划线：$wiki" >&2; exit 2 ;;
  esac
  for dir in "$src"/${wiki:-*}/; do
    [ -f "$dir/chunks.jsonl" ] && [ -f "$dir/cache/listing.json" ] && found=1
  done
  [ "$found" = 1 ] || {
    echo "本机的 $src/${wiki:-<wiki 的 id>}/ 里没有 chunks.jsonl 或 cache/listing.json，没有可拷的知识库" >&2
    exit 1
  }
  data=$(remote "sed -n 's/^JIANSHANAGENT_DATA_DIR=//p' '$ROOT/shared/.env'")
  [ -n "$data" ] || { echo "服务器的 $ROOT/shared/.env 里没有 JIANSHANAGENT_DATA_DIR" >&2; exit 1; }
  dest="$data/bot/kb"
  if [ -n "$wiki" ]; then
    src="$src/$wiki"
    dest="$dest/$wiki"
  fi
  # 拷的时候不能让自动更新同时在写这些文件。当前发布里还没有这个服务（还没上线带自动更新的版本）就跳过
  remote "cd '$ROOT' && ./ctl stop kbsync" 2>/dev/null \
    || echo "（服务器上没有在跑的 kbsync 服务，直接拷）"
  remote "mkdir -p '$dest'"
  rsync -az --stats -e "ssh -o BatchMode=yes" \
    --exclude 'cache/images/files/' --exclude 'cache/sync.lock' --exclude 'sync_heartbeat' \
    --exclude 'eval/' --exclude 'logs/' --exclude 'debug/' --exclude 'export/' \
    --exclude 'backup-*/' --exclude '*.tmp' --exclude '.DS_Store' \
    "$src/" "$HOST:$dest/" | sed -n '/Number of files transferred/p; /Total transferred file size/p'
  remote "chown -R 10001:10001 '$dest'"
  echo "已拷到 $HOST:${dest}（属主 10001）。"
  if remote "cd '$ROOT' && ./ctl up -d kbsync" 2>/dev/null; then
    echo "kbsync 已启动：它会先把这些文件导入数据库，然后每隔一段时间跟着 wiki 更新。"
    echo "看它在做什么：ssh $HOST $ROOT/ctl logs -f --tail 50 kbsync，或者管理后台的知识库页。"
  else
    echo "当前发布里还没有 kbsync 服务：release.sh push / activate 上线带自动更新的版本后，它会自己接着做。"
  fi
}

case "${1:-}" in
  deploy) shift; cmd_deploy "$@" ;;
  pin) cmd_pin ;;
  push) shift; cmd_push "$@" ;;
  activate) shift; cmd_activate "$@" ;;
  rollback) cmd_rollback ;;
  prune) shift; cmd_prune "$@" ;;
  secrets) cmd_secrets ;;
  list) cmd_list ;;
  kb-seed) shift; cmd_kb_seed "$@" ;;
  webui) shift; cmd_webui "$@" ;;
  *)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
