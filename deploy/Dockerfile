# 构建上下文是服务器上的一个发布目录（releases/<发布号>/，release.sh push 拼出来的）：
#   components/core/      核心的源码（pyproject.toml、uv.lock、src/）
#   components/<插件>/     这个部署用到的各个插件的源码
#   config/               这个部署的配置
# 基础镜像只用 python:3.12-slim 一个；uv 用 pip 从镜像源装，不从 ghcr.io 拉，
# 也不用 apt（健康检查用 Python 自己发请求），这样在拉不动外网镜像的服务器上也能构建。
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS builder

# 只在构建阶段生效，不会进最终镜像
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG UV_VERSION=0.10.2
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN pip install "uv==${UV_VERSION}"

WORKDIR /build
COPY components/core/pyproject.toml components/core/uv.lock components/core/README.md ./lock/
# 第三方依赖的版本和哈希都取自核心的 uv.lock，只是下载地址换成镜像源
RUN cd lock \
    && uv export --frozen --no-dev --no-emit-project -o /build/requirements.txt \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --require-hashes -r /build/requirements.txt

COPY components ./components
# 核心和各个插件自己：第三方依赖上一步已经装好，这里不再解析依赖。
# 最后核对一遍：哪个插件要用的包不在核心的锁文件里，在这里就报出来，而不是等到运行时才缺
RUN for dir in components/*/; do \
      uv pip install --python /opt/venv/bin/python --no-deps "./$dir" || exit 1; \
    done \
    && uv pip check --python /opt/venv/bin/python


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    JIANSHANAGENT_CONFIG_DIR=/app/config

RUN useradd --create-home --uid 10001 jianshanagent \
    && mkdir -p /app /data \
    && chown jianshanagent:jianshanagent /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# 这个部署的配置（及实例仓库自带的 sites/ 和 plugins/ 目录）随镜像走。发布里只有 git 提交了的文件，
# 所以没有 secrets.yaml 和 config/local；凭据在运行时由 compose 单独挂载到 /app/config/secrets.yaml
COPY config ./config
COPY sites ./sites
COPY plugins ./plugins

USER jianshanagent
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]

CMD ["jianshanagent"]
