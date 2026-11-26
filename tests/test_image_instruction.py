"""文本指令只影响文本：图片继续使用已有向量、预处理和指纹，全部离线测试。"""

from __future__ import annotations

import argparse
import base64
import io
import json
from array import array
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from jianshanagent.core.config import AppConfig
from jianshanagent.core.embedding import EmbeddingClient, EmbeddingProvider, EmbeddingSettings
from jianshanagent.core.images import normalize_image
from jianshanagent.core.plugin import PluginContext
from jianshanagent.core.wiki import WikiSite
from jianshanagent.plugins.knowledge import image_cli, plugin, sync
from jianshanagent.plugins.knowledge.embed import VectorStore, VectorStoreError
from jianshanagent.plugins.knowledge.image_collect import IMAGES_FILE, original_path, write_images
from jianshanagent.plugins.knowledge.image_embed import (
    embed_image_files,
    embed_key,
    image_status,
    images_without_vectors,
    load_images_into,
    read_image_records,
    vectors_dirname,
    verify_image_vectors,
)
from jianshanagent.plugins.knowledge.image_store import KbImage, MemoryImageStore
from jianshanagent.plugins.knowledge.images import ImageSettings
from jianshanagent.plugins.knowledge.settings import (
    ChunkSettings,
    CleanSettings,
    DateSettings,
    FetchSettings,
    ReportSettings,
    Settings,
    SourceSettings,
)
from jianshanagent.plugins.knowledge.store import MemoryKnowledgeStore

INSTRUCTION = "为检索任务生成文本向量。"
LEGACY_FP = "test:multimodal:2"
IMAGES = ImageSettings()
VECTOR = array("f", [1.0, 0.0])


@pytest.fixture
def provider() -> EmbeddingProvider:
    return EmbeddingProvider(
        name="test",
        base_url="https://embedding.invalid/v1",
        model="multimodal",
        dimensions=2,
        max_retries=0,
        text_instruction=INSTRUCTION,
    )


def png() -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", (32, 24), (200, 80, 20, 128)).save(output, format="PNG")
    return output.getvalue()


def image(number: int = 0) -> KbImage:
    return KbImage(
        image_id=f"立绘{number}.png", kind="立绘", label=f"角色{number}", sha1=f"sha{number}"
    )


def settings(tmp_path: Path, wiki: str = "main") -> Settings:
    out = tmp_path / wiki
    return Settings(
        wiki=WikiSite("https://wiki.invalid/api.php", "https://wiki.invalid", id=wiki),
        fetch=FetchSettings(),
        sources=SourceSettings(),
        clean=CleanSettings(),
        chunk=ChunkSettings(),
        report=ReportSettings(),
        dates=DateSettings(),
        output_dir=out,
        cache_dir=out / "cache",
    )


def legacy_vectors(
    configured: Settings, provider: EmbeddingProvider, count: int = 1
) -> tuple[Path, Path, list[KbImage]]:
    images = [image(i) for i in range(count)]
    path = configured.output_dir / IMAGES_FILE
    write_images(path, images)
    directory = configured.vectors_dir / vectors_dirname(provider)
    store = VectorStore(directory, provider.for_images(), embedded="image")
    store.open()
    try:
        store.append(
            [(item.image_id, embed_key(item, IMAGES.preprocess), VECTOR) for item in images]
        )
    finally:
        store.close()
    return path, directory, images


def file_contents(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def response(request: httpx.Request, bodies: list[dict[str, Any]]) -> httpx.Response:
    body = json.loads(request.content)
    bodies.append(body)
    return httpx.Response(
        200,
        json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(body["input"]))]},
    )


async def test_image_api_and_preprocessing_stay_identical(
    tmp_path: Path, provider: EmbeddingProvider
) -> None:
    configured = settings(tmp_path)
    item = image()
    path = configured.output_dir / IMAGES_FILE
    write_images(path, [item])
    original = original_path(configured.cache_dir, item)
    original.parent.mkdir(parents=True)
    original.write_bytes(png())
    bodies: list[dict[str, Any]] = []
    transport = httpx.MockTransport(lambda request: response(request, bodies))
    for name, current in (("old", provider.for_images()), ("instruction", provider)):
        directory = tmp_path / name
        result = await embed_image_files(
            current, path, directory, configured.cache_dir, IMAGES, transport=transport
        )
        assert result.report.embedded == 1
        assert result.report.fingerprint == LEGACY_FP
        verified = await verify_image_vectors(
            current, path, directory, configured.cache_dir, IMAGES, sample=1, transport=transport
        )
        assert verified.checked == 1
        assert json.loads((directory / "meta.json").read_text())["fingerprint"] == LEGACY_FP
    assert len(bodies) == 4 and all(body == bodies[0] for body in bodies)
    jpeg = normalize_image(png(), max_side=IMAGES.max_side, quality=IMAGES.jpeg_quality)
    assert bodies[0] == {
        "model": "multimodal",
        "dimensions": 2,
        "encoding_format": "base64",
        "input": [{"image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}],
    }
    assert provider.text_instruction == INSTRUCTION and provider.fingerprint != LEGACY_FP


async def test_old_2972_image_vectors_reused_without_originals_or_api(
    tmp_path: Path, provider: EmbeddingProvider
) -> None:
    configured = settings(tmp_path)
    path, directory, images = legacy_vectors(configured, provider, count=2972)
    before = file_contents(directory)
    assert images_without_vectors(provider, images, directory, IMAGES) == []
    status = image_status(provider, path, directory, configured.cache_dir, IMAGES)
    assert status.vectors.already == 2972 and status.vectors.missing == 0
    assert status.vectors.fingerprint == LEGACY_FP and status.no_original == 2972
    blocked = AsyncMock(side_effect=AssertionError("不应调用图片接口"))
    result = await embed_image_files(
        provider,
        path,
        directory,
        configured.cache_dir,
        IMAGES,
        transport=httpx.MockTransport(blocked),
    )
    assert result.report.already == 2972 and result.report.embedded == 0
    assert result.no_original == [] and blocked.await_count == 0
    assert file_contents(directory) == before
    records = list(read_image_records(path, directory, provider, IMAGES.preprocess, "main"))
    assert len(records) == 2972 and records[0] == (images[0].of("main"), VECTOR)


async def test_image_import_keeps_fingerprint_rows_and_other_wikis(
    tmp_path: Path, provider: EmbeddingProvider
) -> None:
    configured = settings(tmp_path)
    path, directory, _ = legacy_vectors(configured, provider)
    store = MemoryImageStore()
    await store.sync(
        [(image(2).of("other"), VECTOR)],
        fingerprint="other:model:2",
        preprocess=IMAGES.preprocess,
        wiki="other",
    )
    first = await load_images_into(
        store, path, directory, provider.for_images(), IMAGES.preprocess, wiki="main"
    )
    before = await store.info()
    hashes = dict(store._hashes)
    again = await load_images_into(store, path, directory, provider, IMAGES.preprocess, wiki="main")
    assert first.upserted == 1 and again.upserted == 0 and again.unchanged == 1
    assert again.with_vectors == 1 and again.deleted == 0
    assert await store.info() == before and store._hashes == hashes
    assert before.wikis["main"].fingerprint == LEGACY_FP
    assert before.wikis["other"].fingerprint == "other:model:2"


async def test_auto_sync_reuses_old_images_without_download_or_rebuild(
    tmp_path: Path, provider: EmbeddingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(tmp_path)
    path, directory, _ = legacy_vectors(configured, provider, count=2972)
    before = file_contents(directory)
    store = MemoryImageStore()
    await load_images_into(
        store, path, directory, provider.for_images(), IMAGES.preprocess, wiki="main"
    )
    env = sync.SyncEnv(
        settings=configured,
        images=IMAGES,
        provider=provider,
        targets=sync.Targets(MemoryKnowledgeStore(), store, sync.MemoryStatusStore()),
    )
    download = AsyncMock(side_effect=AssertionError("旧向量齐全，不应下载"))
    embed = AsyncMock(side_effect=AssertionError("旧向量齐全，不应重算"))
    monkeypatch.setattr(sync, "download_originals", download)
    monkeypatch.setattr(sync, "embed_image_files", embed)
    state = sync.SyncState(images_embed_pending=True)
    loaded: dict[str, str] = {}
    assert sync._image_paths(env) == (path, directory)
    assert await sync._images_vectors(env, state, loaded, force=False) == {}
    assert not state.images_embed_pending and download.await_count == embed.await_count == 0
    result = await sync._images_load(env, loaded, force=False)
    assert result == {"upserted": 0, "deleted": 0, "without_vectors": 0}
    assert await sync._images_load(env, loaded, force=False) == {}
    assert file_contents(directory) == before
    assert (await store.info()).wikis["main"].fingerprint == LEGACY_FP
    assert env.provider is provider and env.provider.fingerprint != LEGACY_FP
    assert env.image_provider.fingerprint == LEGACY_FP


@pytest.mark.parametrize("change", [{"model": "another"}, {"dimensions": 3}])
async def test_real_image_model_mismatch_still_rejected(
    tmp_path: Path, provider: EmbeddingProvider, change: dict[str, Any]
) -> None:
    configured = settings(tmp_path)
    path, directory, _ = legacy_vectors(configured, provider)
    changed = replace(provider, **change)
    with pytest.raises(VectorStoreError, match="不是同一种"):
        image_status(changed, path, directory, configured.cache_dir, IMAGES)
    store = MemoryImageStore()
    await load_images_into(store, path, directory, provider, IMAGES.preprocess, wiki="main")
    env = sync.SyncEnv(
        configured,
        IMAGES,
        changed,
        targets=sync.Targets(MemoryKnowledgeStore(), store, sync.MemoryStatusStore()),
    )
    with pytest.raises(sync.Held, match="不是同一种"):
        await sync._images_load(env, {}, force=False)


@pytest.mark.parametrize("instruction", ["", INSTRUCTION])
def test_image_cli_uses_old_provider_path_and_does_not_mutate_text_config(
    tmp_path: Path, provider: EmbeddingProvider, instruction: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = replace(provider, text_instruction=instruction)
    embedding = EmbeddingSettings({"test": current}, "test")
    monkeypatch.setattr(EmbeddingSettings, "from_config", lambda config: embedding)
    configured = settings(tmp_path)
    path, directory, _ = legacy_vectors(configured, current)
    found = image_cli._provider_and_paths(
        argparse.Namespace(provider=None, output_dir=None),
        AppConfig(tmp_path, {}, {}),
        configured,
    )
    assert found == (current.for_images(), path, directory)
    assert current.text_instruction == instruction
    assert found is not None and found[0].fingerprint == LEGACY_FP


async def test_plugin_image_search_uses_legacy_fp_but_text_keeps_instruction(
    tmp_path: Path, provider: EmbeddingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(
        tmp_path,
        {
            "core.wiki": {
                "wikis": [
                    {"id": "main", "api_url": "https://main.invalid/api.php"},
                    {"id": "other", "api_url": "https://other.invalid/api.php"},
                ]
            },
            "plugin.knowledge": {"retrieval": {"enabled": True}},
        },
        {},
    )
    monkeypatch.setattr(
        EmbeddingSettings,
        "from_config",
        lambda config: EmbeddingSettings({"test": provider}, "test"),
    )
    bodies: list[dict[str, Any]] = []
    transport = httpx.MockTransport(lambda request: response(request, bodies))
    monkeypatch.setattr(
        plugin, "EmbeddingClient", lambda current: EmbeddingClient(current, transport=transport)
    )
    store = MemoryImageStore()
    for wiki, fp in (("main", LEGACY_FP), ("other", "incompatible:model:2")):
        await store.sync(
            [(image().of(wiki), VECTOR)], fingerprint=fp, preprocess=IMAGES.preprocess, wiki=wiki
        )
    monkeypatch.setattr(plugin, "PostgresImageStore", lambda db: store)
    monkeypatch.setattr(plugin, "PostgresKnowledgeStore", lambda db: MemoryKnowledgeStore())
    services: dict[str, Any] = {"database": object()}
    plugin.KnowledgePlugin().register(PluginContext("knowledge", config, services))
    search, text = services["image_search"], services["knowledge"]
    try:
        assert text.embedder.provider.fingerprint == provider.fingerprint != LEGACY_FP
        assert text.embedder.provider.text_instruction == INSTRUCTION
        assert search.embedder.provider.fingerprint == LEGACY_FP
        hits = await search.search(png())
        assert [(hit.wiki, hit.image_id) for hit in hits] == [("main", "main:立绘0.png")]
        assert "instruction" not in bodies[-1]
        await text.embedder.embed_one("查询文字")
        assert bodies[-1]["instruction"] == INSTRUCTION
        assert bodies[-1]["input"] == ["查询文字"]
        assert (await store.info()).wikis["other"].fingerprint == "incompatible:model:2"
    finally:
        await search.aclose()
        await text.aclose()
