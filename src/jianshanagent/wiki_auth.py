"""页面上保存的 wiki 放行密钥。

``secrets.yaml`` 在容器里是只读的。页面上填的存在数据目录，盖过
``secrets.yaml`` 里同一个 wiki 的 ``authkey``。文件权限 600，接口不把密钥读回去。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

_WIKI_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: 可见 ASCII、不含空格。放行密钥是请求头的值，空格和换行会把请求头弄坏
_AUTHKEY = re.compile(r"^[!-~]{1,256}$")


class WikiAuthStore:
    def __init__(self, root: Path):
        self.root = root

    def directory(self) -> Path:
        configured = os.environ.get("JIANSHANAGENT_WIKI_AUTH_DIR", "").strip()
        return Path(configured) if configured else self.root.parent / "data" / "wiki-auth"

    def _path(self, wiki_id: str) -> Path:
        if not _WIKI_ID.fullmatch(wiki_id):
            raise ValueError("wiki 的名字不合法")
        return self.directory() / f"{wiki_id}.json"

    def saved(self, wiki_id: str) -> str | None:
        """页面上保存的密钥。没有、读不了、或者内容不像密钥，都当没保存。"""
        try:
            data = json.loads(self._path(wiki_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        key = str(data.get("authkey") or "") if isinstance(data, dict) else ""
        return key if _AUTHKEY.fullmatch(key) else None

    def set(self, wiki_id: str, authkey: str) -> None:
        """保存。空字符串删掉页面上的这一份，改回用 ``secrets.yaml``。"""
        path = self._path(wiki_id)
        if not authkey:
            path.unlink(missing_ok=True)
            return
        if not _AUTHKEY.fullmatch(authkey):
            raise ValueError("放行密钥只能是可见的英文字符，不能有空格，最多 256 个")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{wiki_id}.", delete=False
        )
        try:
            with temporary as file:
                json.dump({"authkey": authkey}, file, ensure_ascii=False)
                file.write("\n")
            os.chmod(temporary.name, 0o600)
            os.replace(temporary.name, path)
        finally:
            try:
                os.unlink(temporary.name)
            except FileNotFoundError:
                pass

    def snapshot(self, wiki_id: str) -> bytes | None:
        try:
            return self._path(wiki_id).read_bytes()
        except OSError:
            return None

    def restore(self, wiki_id: str, previous: bytes | None) -> None:
        path = self._path(wiki_id)
        if previous is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(previous)
        os.chmod(path, 0o600)
