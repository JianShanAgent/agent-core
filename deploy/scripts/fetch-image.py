#!/usr/bin/env python3
"""不经过 Docker 守护进程，直接从镜像站把一个镜像下载成 OCI 包，再用 docker load 导入。

为什么需要：服务器的 Docker 守护进程配置了 HTTP 代理，`docker pull` 走代理访问镜像站的
对象存储地址时会 EOF；而服务器直连这些镜像站是通的。守护进程的配置不能动（重启 Docker
会影响别的项目），所以改为自己下载。只用标准库，不走任何代理。

用法（在服务器上）：
    python3 fetch-image.py docker.m.daocloud.io/pgvector/pgvector:pg17 \
        pgvector/pgvector:pg17 /tmp/pgvector-pg17.tar
    docker load -i /tmp/pgvector-pg17.tar

参数：<镜像站地址/仓库:标签> <导入后的镜像名> <输出 tar 路径>
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PLATFORM = ("linux", "amd64")
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
# 不读环境变量里的代理设置
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# 镜像站的对象存储会拒绝 Python 默认的 User-Agent（返回 403），所以自报一个名字
OPENER.addheaders = [("User-Agent", "jianshanagent-fetch-image/1.0")]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Registry:
    def __init__(self, host: str, repo: str):
        self.host = host
        self.repo = repo
        self.token: str | None = None

    def _request(self, url: str, headers: dict[str, str]) -> urllib.request.Request:
        req = urllib.request.Request(url, headers=headers)
        if self.token:
            # 令牌只发给镜像站本身；跳转到对象存储时不带（否则预签名地址会拒绝）
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return req

    def _login(self, challenge: str) -> None:
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = fields.pop("realm")
        query = "&".join(f"{k}={urllib.request.quote(v, safe='')}" for k, v in fields.items())
        with OPENER.open(f"{realm}?{query}", timeout=30) as resp:
            body = json.load(resp)
        self.token = body.get("token") or body.get("access_token")

    def open(self, path: str, headers: dict[str, str] | None = None):
        url = f"https://{self.host}/v2/{self.repo}/{path}"
        headers = dict(headers or {})
        for attempt in (1, 2):
            try:
                return OPENER.open(self._request(url, headers), timeout=60)
            except urllib.error.HTTPError as err:
                challenge = err.headers.get("WWW-Authenticate", "")
                if err.code == 401 and attempt == 1 and challenge.lower().startswith("bearer"):
                    self._login(challenge)
                    continue
                raise
        raise RuntimeError("unreachable")

    def manifest(self, reference: str) -> tuple[bytes, str]:
        with self.open(f"manifests/{reference}", {"Accept": ACCEPT}) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")

    def blob(self, digest: str, dest: Path, size: int) -> None:
        """下载一个 blob：支持断点续传，下载完校验 sha256。"""
        algo, hexdigest = digest.split(":", 1)
        for attempt in range(1, 9):
            have = dest.stat().st_size if dest.exists() else 0
            if have < size:
                headers = {"Range": f"bytes={have}-"} if have else {}
                try:
                    with self.open(f"blobs/{digest}", headers) as resp:
                        if have and resp.status != 206:
                            have = 0  # 服务端不支持续传，从头来
                        with dest.open("ab" if have else "wb") as out:
                            while chunk := resp.read(1 << 20):
                                out.write(chunk)
                except (urllib.error.URLError, OSError, TimeoutError) as err:
                    log(f"  {digest[:19]} 第 {attempt} 次中断：{err}；稍后续传")
                    time.sleep(min(2 * attempt, 15))
                    continue
            hasher = hashlib.new(algo)
            with dest.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    hasher.update(chunk)
            if hasher.hexdigest() == hexdigest:
                return
            log(f"  {digest[:19]} 校验不一致，重新下载")
            dest.unlink()
        raise SystemExit(f"下载失败：{digest}")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    source, target_name, out_path = sys.argv[1:]
    host, _, rest = source.partition("/")
    repo, _, tag = rest.rpartition(":")
    if not (host and repo and tag):
        raise SystemExit("来源要写成 <镜像站>/<仓库>:<标签>")
    if "/" not in target_name.split(":")[0]:
        target_name = f"library/{target_name}"
    full_name = f"docker.io/{target_name}" if target_name.count("/") == 1 else target_name

    work = Path(out_path + ".parts")
    blobs = work / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    reg = Registry(host, repo)

    raw, media_type = reg.manifest(tag)
    doc = json.loads(raw)
    if "manifests" in doc:  # 多架构索引：挑 linux/amd64
        chosen = next(
            m
            for m in doc["manifests"]
            if (m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture"))
            == PLATFORM
        )
        raw, media_type = reg.manifest(chosen["digest"])
        doc = json.loads(raw)
    media_type = doc.get("mediaType") or media_type
    manifest_digest = hashlib.sha256(raw).hexdigest()
    (blobs / manifest_digest).write_bytes(raw)
    log(f"清单 sha256:{manifest_digest}，{len(doc['layers'])} 层")

    for item in [doc["config"], *doc["layers"]]:
        log(f"  下载 {item['digest'][:19]} {item['size'] / 1e6:.1f} MB")
        reg.blob(item["digest"], blobs / item["digest"].split(":", 1)[1], item["size"])

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": media_type,
                "digest": f"sha256:{manifest_digest}",
                "size": len(raw),
                "platform": {"os": PLATFORM[0], "architecture": PLATFORM[1]},
                "annotations": {
                    "io.containerd.image.name": full_name,
                    "org.opencontainers.image.ref.name": full_name.rsplit(":", 1)[1],
                },
            }
        ],
    }
    with tarfile.open(out_path, "w") as tar:

        def add_bytes(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add_bytes("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add_bytes("index.json", json.dumps(index).encode())
        for path in sorted(blobs.iterdir()):
            tar.add(path, arcname=f"blobs/sha256/{path.name}")
    for path in blobs.iterdir():
        path.unlink()
    blobs.rmdir()
    (work / "blobs").rmdir()
    work.rmdir()
    log(f"已写出 {out_path}，导入后的名字：{full_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
