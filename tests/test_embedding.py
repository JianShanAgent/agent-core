"""嵌入调用层和「给切片算向量」的任务：解析、顺序、重试、断点续跑。不联网。"""

import base64
import hashlib
import json
import math
import struct
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from jianshanagent.core.config import AppConfig
from jianshanagent.core.embedding import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingSettings,
    dot,
)
from jianshanagent.plugins.knowledge.embed import (
    VECTORS_FILE,
    VectorStore,
    VectorStoreError,
    embed_chunks,
    store_status,
    text_hash,
    verify_store,
)

DIM = 4
PROVIDER = EmbeddingProvider(
    name="fake", base_url="https://e/v1", model="m", dimensions=DIM, batch_size=3, max_retries=2
)


def vector_for(text: str) -> list[float]:
    """每段文字一个固定的、互不相同的单位向量。"""
    seed = sum(text.encode("utf-8")) % 97 + 1
    raw = [math.sin(seed * (i + 1)) for i in range(DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def b64(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode()


def fake_api(calls: list[list[str]], *, fail: set[str] | None = None, mutate=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        texts = body["input"]
        calls.append(texts)
        assert body["dimensions"] == DIM and body["model"] == "m"
        if fail and fail & set(texts):
            return httpx.Response(422, json={"error": "bad input"})
        data = [{"index": i, "embedding": b64(vector_for(t))} for i, t in enumerate(texts)]
        payload = {"data": data, "usage": {"prompt_tokens": 10 * len(texts)}}
        return httpx.Response(200, json=mutate(payload) if mutate else payload)

    return httpx.MockTransport(handler)


def client(transport: httpx.MockTransport) -> EmbeddingClient:
    return EmbeddingClient(PROVIDER, transport=transport, retry_base_seconds=0)


@pytest.mark.parametrize(
    "instruction",
    [
        "",
        "Represent the user's input.",
        "表示用户输入。",
        "表示用户输入!",
        " 表示用户输入。",
        "表示用户输入。 ",
        " ",
    ],
)
def test_provider_fingerprint_includes_exact_utf8_instruction(instruction: str):
    provider = replace(PROVIDER, text_instruction=instruction)
    expected = "fake:m:4"
    if instruction:
        expected += ":" + hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        assert len(expected.rsplit(":", 1)[1]) == 64
    assert provider.fingerprint == expected


def test_different_text_instructions_have_different_fingerprints():
    instructions = ["", "表示用户输入。", "表示用户输入!", " 表示用户输入。", "表示用户输入。 "]
    fingerprints = {
        replace(PROVIDER, text_instruction=instruction).fingerprint for instruction in instructions
    }
    assert len(fingerprints) == len(instructions)


def test_for_images_only_clears_text_instruction_and_preserves_legacy_fingerprint():
    provider = EmbeddingProvider(
        name="custom",
        base_url="https://e/v1",
        model="image-model",
        api_key="test-key",
        dimensions=8,
        batch_size=7,
        concurrency=3,
        timeout_seconds=42.5,
        max_retries=4,
        text_instruction="Represent the user's input.",
    )
    image_provider = provider.for_images()
    assert image_provider == replace(provider, text_instruction="")
    assert image_provider is not provider
    assert provider.text_instruction == "Represent the user's input."
    assert image_provider.fingerprint == "custom:image-model:8"
    assert image_provider.fingerprint != provider.fingerprint
    assert image_provider.for_images() is image_provider
    assert PROVIDER.for_images() is PROVIDER


@pytest.mark.parametrize("instruction", ["", "Represent the user's input.", " 表示用户输入。 "])
async def test_text_instruction_is_top_level_and_consistent_for_batch_and_single(instruction: str):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        data = [
            {"index": i, "embedding": b64(vector_for(text))} for i, text in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": list(reversed(data))})

    embedding_client = EmbeddingClient(
        replace(PROVIDER, text_instruction=instruction), transport=httpx.MockTransport(handler)
    )
    try:
        result = await embedding_client.embed(["甲", "乙"])
        single = await embedding_client.embed_one("甲")
        await embedding_client.embed(["甲"])
        assert (await embedding_client.embed([])).vectors == []
    finally:
        await embedding_client.aclose()

    expected = {
        "model": "m",
        "input": ["甲", "乙"],
        "dimensions": DIM,
        "encoding_format": "base64",
    }
    if instruction:
        expected["instruction"] = instruction
    assert bodies == [expected, {**expected, "input": ["甲"]}, {**expected, "input": ["甲"]}]
    assert dot(single, result.vectors[0]) == pytest.approx(1.0, abs=1e-5)
    for text, vector in zip(["甲", "乙"], result.vectors, strict=True):
        assert dot(vector, vector_for(text)) == pytest.approx(1.0, abs=1e-5)


async def test_embed_returns_vectors_in_input_order_even_if_index_restarts():
    def restart_index(payload):
        # 硅基流动的毛病：index 每隔几条从 0 重数，但返回顺序是对的
        for i, item in enumerate(payload["data"]):
            item["index"] = i % 2
        return payload

    texts = ["甲", "乙", "丙"]
    result = await client(fake_api([], mutate=restart_index)).embed(texts)
    for text, vector in zip(texts, result.vectors, strict=True):
        assert dot(vector, vector_for(text)) == pytest.approx(1.0, abs=1e-5)
    assert result.prompt_tokens == 30


async def test_embed_sorts_by_index_when_it_is_a_real_permutation():
    def shuffle(payload):
        payload["data"].reverse()
        return payload

    texts = ["甲", "乙", "丙"]
    result = await client(fake_api([], mutate=shuffle)).embed(texts)
    for text, vector in zip(texts, result.vectors, strict=True):
        assert dot(vector, vector_for(text)) == pytest.approx(1.0, abs=1e-5)


async def test_embed_accepts_float_lists_and_normalizes():
    def as_unnormalized_floats(payload):
        for item in payload["data"]:
            raw = base64.b64decode(item["embedding"])
            item["embedding"] = [x * 0.5 for x in struct.unpack(f"<{DIM}f", raw)]
        return payload

    vector = await client(fake_api([], mutate=as_unnormalized_floats)).embed_one("甲")
    assert dot(vector, vector) == pytest.approx(1.0, abs=1e-5)
    assert dot(vector, vector_for("甲")) == pytest.approx(1.0, abs=1e-5)


async def test_embed_rejects_wrong_count_and_wrong_dimensions():
    def drop_one(payload):
        payload["data"].pop()
        return payload

    def truncate(payload):
        for item in payload["data"]:
            item["embedding"] = b64([1.0, 0.0])
        return payload

    with pytest.raises(EmbeddingError, match="返回了 1 个向量"):
        await client(fake_api([], mutate=drop_one)).embed(["甲", "乙"])
    with pytest.raises(EmbeddingError, match="2 维"):
        await client(fake_api([], mutate=truncate)).embed(["甲"])


async def test_embed_retries_transient_errors_but_not_permanent_ones():
    seen = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] <= 2:  # 模力方舟偶发的 400 和常见的 503 都重试
            return httpx.Response(400 if seen["n"] == 1 else 503, text="busy")
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": b64(vector_for("甲"))}]}
        )

    vector = await client(httpx.MockTransport(flaky)).embed_one("甲")
    assert seen["n"] == 3 and len(vector) == DIM

    seen["n"] = 0

    def denied(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(EmbeddingError) as info:
        await client(httpx.MockTransport(denied)).embed_one("甲")
    assert info.value.status == 401 and seen["n"] == 1


@pytest.mark.parametrize(
    ("instruction", "use_image_provider"),
    [("", False), ("Represent the user's input.", False), ("Represent the user's input.", True)],
)
async def test_embed_images_sends_the_pictures_inline_and_keeps_their_order(
    instruction: str, use_image_provider: bool
):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        data = [
            {"index": i, "embedding": b64(vector_for(item["image"]))}
            for i, item in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 7}})

    provider = replace(PROVIDER, text_instruction=instruction)
    if use_image_provider:
        provider = provider.for_images()
        assert provider.fingerprint == PROVIDER.fingerprint
    embedding_client = EmbeddingClient(provider, transport=httpx.MockTransport(handler))
    images = [b"\xff\xd8first", b"\xff\xd8second"]
    result = await embedding_client.embed_images(images)
    # 图片内容编成 base64 放在请求里（对方不会自己去下载图片地址），和文字一样每次都带 dimensions
    uris = ["data:image/jpeg;base64," + base64.b64encode(image).decode() for image in images]
    assert bodies[0]["input"] == [{"image": uri} for uri in uris]
    assert bodies[0]["dimensions"] == DIM and bodies[0]["model"] == "m"
    for uri, vector in zip(uris, result.vectors, strict=True):
        assert dot(vector, vector_for(uri)) == pytest.approx(1.0, abs=1e-5)
    assert result.prompt_tokens == 7

    one = await embedding_client.embed_image(images[1])
    assert dot(one, vector_for(uris[1])) == pytest.approx(1.0, abs=1e-5)
    assert (await embedding_client.embed_images([])).vectors == []
    await embedding_client.aclose()
    assert len(bodies) == 2  # 没有图片就不发请求
    assert bodies[0] == {
        "model": "m",
        "input": [{"image": uri} for uri in uris],
        "dimensions": DIM,
        "encoding_format": "base64",
    }
    assert bodies[1] == {**bodies[0], "input": [{"image": uris[1]}]}


def test_settings_read_providers_and_keys(tmp_path: Path):
    files = {
        "core.embedding": {
            "enabled": True,
            "default_provider": "gitee",
            "providers": {
                "gitee": {"base_url": "https://g/v1/", "model": "M", "secret": "gitee_ai"},
                "local": {"base_url": "http://127.0.0.1:1/v1", "model": "L", "batch_size": 32},
            },
        }
    }
    settings = EmbeddingSettings.from_config(
        AppConfig(tmp_path, files, {"gitee_ai": {"api_key": " k "}})
    )
    assert settings is not None
    gitee = settings.provider()
    assert (gitee.base_url, gitee.api_key, gitee.dimensions) == ("https://g/v1", "k", 4096)
    assert gitee.fingerprint == "gitee:M:4096"
    assert gitee.text_instruction == ""
    assert settings.provider("local").batch_size == 32
    assert settings.provider("local").fingerprint != gitee.fingerprint
    with pytest.raises(EmbeddingError, match="没有提供方"):
        settings.provider("nope")
    files["core.embedding"]["enabled"] = False
    assert EmbeddingSettings.from_config(AppConfig(tmp_path, files, {})) is None


@pytest.mark.parametrize("instruction", ["", "Represent the user's input.", " 表示用户输入。 "])
def test_settings_read_text_instruction_without_changing_it(tmp_path: Path, instruction: str):
    files = {
        "core.embedding": {
            "enabled": True,
            "default_provider": "fake",
            "providers": {
                "fake": {
                    "base_url": "https://e/v1",
                    "model": "m",
                    "dimensions": DIM,
                    "text_instruction": instruction,
                }
            },
        }
    }
    settings = EmbeddingSettings.from_config(AppConfig(tmp_path, files, {}))
    assert settings is not None
    provider = settings.provider()
    assert provider.text_instruction == instruction
    assert provider.fingerprint == replace(PROVIDER, text_instruction=instruction).fingerprint
    assert provider.for_images().fingerprint == PROVIDER.fingerprint


@pytest.mark.parametrize("instruction", [None, False, 0, 1.5, [], {}])
def test_settings_reject_non_string_text_instruction(tmp_path: Path, instruction):
    files = {
        "core.embedding": {
            "enabled": True,
            "default_provider": "fake",
            "providers": {
                "fake": {
                    "base_url": "https://e/v1",
                    "model": "m",
                    "text_instruction": instruction,
                }
            },
        }
    }
    with pytest.raises(EmbeddingError, match=r"providers\.fake\.text_instruction 必须是字符串"):
        EmbeddingSettings.from_config(AppConfig(tmp_path, files, {}))


def write_chunks(path: Path, texts: dict[str, str], *, skip: set[str] | None = None) -> Path:
    with path.open("w", encoding="utf-8") as out:
        for chunk_id, text in texts.items():
            row = {"chunk_id": chunk_id, "text": text, "embed": chunk_id not in (skip or set())}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


TEXTS = {f"c{i}": f"第 {i} 片" + "字" * i for i in range(8)}


async def run(tmp_path: Path, calls: list[list[str]], **kwargs):
    chunks = tmp_path / "chunks.jsonl"
    if not chunks.exists():
        write_chunks(chunks, TEXTS)
    fail = kwargs.pop("fail", None)
    return await embed_chunks(
        PROVIDER,
        chunks,
        tmp_path / "vectors",
        transport=fake_api(calls, fail=fail),
        retry_base_seconds=0,
        **kwargs,
    )


def stored_vectors(tmp_path: Path) -> dict[str, list[float]]:
    store = VectorStore(tmp_path / "vectors", PROVIDER)
    store.open()
    store.close()
    return {chunk_id: list(store.vector(i)) for i, (chunk_id, _) in enumerate(store.rows)}


async def test_embed_chunks_stores_every_vector_under_the_right_chunk(tmp_path: Path):
    calls: list[list[str]] = []
    report = await run(tmp_path, calls)
    assert (report.total, report.already, report.embedded, report.missing) == (8, 0, 8, 0)
    assert report.prompt_tokens == 80 and not report.failed
    assert [len(c) for c in calls] == [3, 3, 2]
    assert len(calls[0][0]) >= len(calls[-1][-1])  # 长的先算
    vectors = stored_vectors(tmp_path)
    assert set(vectors) == set(TEXTS)
    for chunk_id, vector in vectors.items():
        assert dot(vector, vector_for(TEXTS[chunk_id])) == pytest.approx(1.0, abs=1e-5)


async def test_embed_chunks_resumes_and_only_redoes_changed_text(tmp_path: Path):
    calls: list[list[str]] = []
    first = await run(tmp_path, calls, limit=5)
    assert (first.embedded, first.missing) == (5, 3)

    # 模拟中断：向量文件末尾多了半条没有索引的向量
    with (tmp_path / "vectors" / VECTORS_FILE).open("ab") as fh:
        fh.write(b"\x00" * 7)
    calls.clear()
    second = await run(tmp_path, calls)
    assert (second.already, second.embedded, second.missing) == (5, 3, 0)
    assert sum(len(c) for c in calls) == 3

    # 一片的文字变了、一片不再需要算：只重算变了的那片，旧向量留着但数成「旧的」
    changed = {**TEXTS, "c2": "改过的文字"}
    write_chunks(tmp_path / "chunks.jsonl", changed, skip={"c7"})
    calls.clear()
    third = await run(tmp_path, calls)
    assert calls == [["改过的文字"]]
    assert (third.total, third.already, third.embedded, third.stale) == (7, 6, 1, 2)
    status = store_status(PROVIDER, tmp_path / "chunks.jsonl", tmp_path / "vectors")
    assert (status.total, status.already, status.missing, status.stale) == (7, 7, 0, 2)

    store = VectorStore(tmp_path / "vectors", PROVIDER)
    store.open()
    store.close()
    row = store.row("c2", text_hash("改过的文字"))
    assert row is not None
    assert dot(store.vector(row), vector_for("改过的文字")) == pytest.approx(1.0, abs=1e-5)


async def test_embed_chunks_keeps_going_after_a_failed_batch(tmp_path: Path):
    calls: list[list[str]] = []
    report = await run(tmp_path, calls, fail={TEXTS["c7"]})
    assert len(report.failed) == 3 and "c7" in report.failed and not report.stopped
    assert (report.embedded, report.missing) == (5, 3)
    assert "422" in report.errors[0]

    calls.clear()
    again = await run(tmp_path, calls)  # 接口好了以后重跑，只补没算成的
    assert (again.already, again.embedded, again.missing) == (5, 3, 0)


async def test_embed_chunks_stops_after_consecutive_failures(tmp_path: Path):
    calls: list[list[str]] = []
    write_chunks(tmp_path / "chunks.jsonl", {f"c{i}": f"片{i}" for i in range(30)})
    report = await run(tmp_path, calls, fail={f"片{i}" for i in range(30)})
    assert report.stopped and report.embedded == 0
    assert len(calls) == 3  # 连续 3 批失败就停，不把 10 批都试一遍


async def test_store_refuses_vectors_with_another_fingerprint(tmp_path: Path):
    await run(tmp_path, [])
    other = EmbeddingProvider(name="other", base_url="https://o/v1", model="m", dimensions=DIM)
    with pytest.raises(VectorStoreError, match="不是同一种"):
        VectorStore(tmp_path / "vectors", other).open()


async def test_store_can_only_be_opened_by_one_at_a_time(tmp_path: Path):
    await run(tmp_path, [])
    chunks, vectors = tmp_path / "chunks.jsonl", tmp_path / "vectors"
    writing = VectorStore(vectors, PROVIDER)
    writing.open()
    # 算向量没跑完时再来打开（哪怕只是看进度）会修剪文件，前一个之后写的就对不上了：拒绝
    with pytest.raises(VectorStoreError, match="另一个进程"):
        store_status(PROVIDER, chunks, vectors)
    writing.close()
    assert store_status(PROVIDER, chunks, vectors).already == 8


async def test_verify_store_detects_misaligned_vectors(tmp_path: Path):
    await run(tmp_path, [])
    chunks, vectors = tmp_path / "chunks.jsonl", tmp_path / "vectors"
    good = await verify_store(PROVIDER, chunks, vectors, sample=8, transport=fake_api([]))
    assert good.checked == 8 and good.lowest == pytest.approx(1.0, abs=1e-5)

    # 把向量文件整体错开一条：切片和向量对不上了
    path = vectors / VECTORS_FILE
    raw = path.read_bytes()
    path.write_bytes(raw[DIM * 4 :] + raw[: DIM * 4])
    bad = await verify_store(PROVIDER, chunks, vectors, sample=8, transport=fake_api([]))
    assert bad.lowest < 0.99
