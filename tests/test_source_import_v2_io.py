"""Read-only shard safety and legacy streaming regressions."""

import gzip
import json
import os
import tracemalloc
from array import array

import pytest

from jianshanagent.plugins.knowledge.source_import import (
    MAX_LINE_BYTES,
    SourceImportCapacityError,
    SourceImportError,
)
from jianshanagent.plugins.knowledge.source_import_v2_io import (
    iter_rows,
    read_row,
    read_vector,
    stream_v1_snapshot,
    write_rows,
    write_vector_shard,
)


def test_random_compressed_members_reconstruct_complete_text(tmp_path):
    rows = [
        {"id": str(n), "text": "完整原文\n" * (n + 1), "nodes": [{"id": "2"}]} for n in range(8)
    ]
    path = tmp_path / "rows.gz"
    positions = write_rows(path, iter(rows))
    assert list(iter_rows(path)) == rows
    assert read_row(path, *positions["7"]) == rows[7]
    assert read_row(path, *positions["0"]) == rows[0]
    with pytest.raises(FileExistsError):
        write_rows(path, rows)


def test_readers_never_modify_binary_or_archive(tmp_path):
    path = tmp_path / "vectors.f32"
    positions = write_vector_shard(path, [("a", [1, 0]), ("b", [0, 1])], 2)
    original = path.read_bytes()
    os.chmod(path, 0o444)
    assert read_vector(path, *positions["b"], 2) == array("f", [0, 1])
    assert path.read_bytes() == original
    with pytest.raises(SourceImportError):
        read_vector(path, 10, 8, 2)
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["archive", "vector", "legacy"])
def test_symlink_readers_reject(tmp_path, kind):
    target = tmp_path / "real"
    target.write_bytes(b"{}")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SourceImportError, match="符号链接"):
        if kind == "archive":
            read_row(link, 0, 2)
        elif kind == "vector":
            read_vector(link, 0, 4, 1)
        else:
            list(stream_v1_snapshot(link))


def test_zip_bomb_and_oversize_row_are_bounded(tmp_path):
    path = tmp_path / "bomb.gz"
    raw = gzip.compress(b"x" * (MAX_LINE_BYTES + 1))
    path.write_bytes(raw)
    with pytest.raises(SourceImportCapacityError):
        read_row(path, 0, len(raw))
    with pytest.raises(SourceImportCapacityError):
        list(iter_rows(path))
    with pytest.raises(SourceImportCapacityError):
        write_rows(tmp_path / "large.gz", [{"id": "1", "text": "x" * MAX_LINE_BYTES}])


@pytest.mark.parametrize("values", [[1], [float("nan"), 0], [float("inf"), 0]])
def test_bad_vectors_never_sealed(tmp_path, values):
    with pytest.raises(SourceImportError):
        write_vector_shard(tmp_path / "bad.f32", [("1", values)], 2)


def test_stream_snapshot_supports_unicode_records_across_read_boundaries(tmp_path):
    path = tmp_path / "snapshot.json"
    rows = [{"id": str(n), "text": "完整中文" * 5000} for n in range(3)]
    records = [{"chunk": {"chunk_id": str(n)}, "vector": [0.001] * 4096} for n in range(3)]
    path.write_text(
        json.dumps(
            {"version": 1, "rows": rows, "records": records, "fingerprint": "stable"},
            ensure_ascii=False,
        )
    )
    streamed = list(stream_v1_snapshot(path))
    assert [v for k, v in streamed if k == "row"] == rows
    assert [v for k, v in streamed if k == "record"] == records
    assert streamed[-1] == ("meta", {"fingerprint": "stable"})


@pytest.mark.parametrize(
    "raw",
    [
        '{"rows":[{},],"records":[]}',
        '{"rows":[],"rows":[]}',
        '{"rows":[{"id":"1","id":"2"}]}',
        '{"rows":[',
        '{"rows":[]} trailing',
        '{"rows":["not object"]}',
    ],
)
def test_invalid_legacy_json_rejected(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_text(raw)
    with pytest.raises((SourceImportError, ValueError)):
        list(stream_v1_snapshot(path))


def test_streaming_does_not_materialize_legacy_float_json(tmp_path):
    path = tmp_path / "many.json"
    record = json.dumps({"chunk": {"chunk_id": "1"}, "vector": [0.125] * 4096})
    with path.open("w") as file:
        file.write('{"version":1,"rows":[],"records":[')
        for number in range(180):
            if number:
                file.write(",")
            file.write(record)
        file.write('],"fingerprint":"fixed"}')
    tracemalloc.start()
    try:
        count = sum(kind == "record" for kind, _ in stream_v1_snapshot(path))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert count == 180 and peak < 4 * 1024 * 1024


def test_legacy_read_cap_checked_before_decode(tmp_path):
    path = tmp_path / "sparse.json"
    with path.open("wb") as file:
        file.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(SourceImportCapacityError):
        next(stream_v1_snapshot(path))
