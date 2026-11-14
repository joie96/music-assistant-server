"""Test persistent Enhanced YouTube Music audio caching."""

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from music_assistant.providers.ytmusic_enhanced.file_cache import CachedStreamInfo, FileCache


def download_audio(directory: Path) -> CachedStreamInfo:
    """Write a small complete audio payload into the isolated download directory."""
    audio = directory / "download.webm"
    audio.write_bytes(b"audio")
    return CachedStreamInfo(audio, {"audio_ext": "webm", "filesize": 5})


def test_cache_persists_and_reuses_download(tmp_path: Path) -> None:
    """A restarted cache reuses a complete file without invoking the downloader."""
    cache = FileCache(tmp_path)
    cache.ensure_ready()
    hit = cache.get_or_download("abcdefghijk", download_audio)
    assert hit is not None
    assert hit.audio_path.read_bytes() == b"audio"
    download = Mock(side_effect=AssertionError("Unexpected repeat download"))
    assert FileCache(tmp_path).get_or_download("abcdefghijk", download) == hit
    download.assert_not_called()


def test_parallel_requests_only_download_once(tmp_path: Path) -> None:
    """Concurrent requests share a completed cache entry."""
    cache = FileCache(tmp_path)
    download = Mock(side_effect=download_audio)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(lambda _: cache.get_or_download("abcdefghijk", download), range(2))
        )
    assert results[0] == results[1]
    download.assert_called_once()


def test_different_downloads_do_not_block_existing_cache_hits(tmp_path: Path) -> None:
    """Three network workers may run while a completed cache entry remains readable."""
    cache = FileCache(tmp_path)
    cached = cache.get_or_download("abcdefghijk", download_audio)
    started = threading.Barrier(4)
    release = threading.Event()

    def download(directory: Path) -> CachedStreamInfo:
        started.wait(timeout=5)
        assert release.wait(timeout=5)
        return download_audio(directory)

    with ThreadPoolExecutor(max_workers=4) as workers:
        pending = [
            workers.submit(cache.get_or_download, video_id, download)
            for video_id in ("bcdefghijkl", "cdefghijklm", "defghijklmn")
        ]
        try:
            started.wait(timeout=5)
            assert workers.submit(cache.get_hit, "abcdefghijk").result(timeout=1) == cached
        finally:
            release.set()
        assert all(task.result(timeout=5) is not None for task in pending)
    assert not list(tmp_path.glob(".download-*"))


def test_parallel_publication_preserves_size_limit(tmp_path: Path) -> None:
    """Concurrent completed downloads cannot race past the retained-audio limit."""
    cache = FileCache(tmp_path, max_size_bytes=10)
    started = threading.Barrier(3)

    def download(directory: Path) -> CachedStreamInfo:
        started.wait(timeout=5)
        return download_audio(directory)

    with ThreadPoolExecutor(max_workers=3) as workers:
        list(
            workers.map(
                lambda video_id: cache.get_or_download(video_id, download),
                ("abcdefghijk", "bcdefghijkl", "cdefghijklm"),
            )
        )
    assert sum(audio.stat().st_size for audio in tmp_path.glob("*/audio")) == 10
    assert not list(tmp_path.glob(".download-*"))


def test_cache_evicts_least_recently_used(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The audio size limit evicts old files but leaves unrelated files alone."""
    cache = FileCache(tmp_path, max_size_bytes=10)
    caplog.set_level(logging.DEBUG, logger="music_assistant.providers.ytmusic_enhanced.file_cache")
    (tmp_path / "unrelated.txt").write_text("keep", encoding="utf-8")
    cache.get_or_download("abcdefghijk", download_audio)
    cache.get_or_download("lmnopqrstuv", download_audio)
    os.utime(tmp_path / "lmnopqrstuv" / "metadata.json", ns=(1, 1))
    cache.get_or_download("12345678901", download_audio)
    assert cache.get_hit("abcdefghijk") is not None
    assert cache.get_hit("lmnopqrstuv") is None
    assert cache.get_hit("12345678901") is not None
    assert (tmp_path / "unrelated.txt").read_text(encoding="utf-8") == "keep"
    assert "Evicted audio cache entry lmnopqrstuv (5 bytes)" in caplog.text


def test_oversized_download_is_not_retained(tmp_path: Path) -> None:
    """One large file cannot evict useful entries or exceed the audio size limit."""
    cache = FileCache(tmp_path, max_size_bytes=4)
    assert cache.get_or_download("abcdefghijk", download_audio) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("video_id", ["../escape", "", "abcdefghijk/../../outside"])
def test_cache_rejects_invalid_ids(tmp_path: Path, video_id: str) -> None:
    """IDs cannot address files outside the cache."""
    with pytest.raises(ValueError, match="Invalid YouTube"):
        FileCache(tmp_path).get_hit(video_id)


def test_truncated_cache_is_redownloaded(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Changed byte counts invalidate an otherwise published entry."""
    cache = FileCache(tmp_path)
    caplog.set_level(logging.DEBUG, logger="music_assistant.providers.ytmusic_enhanced.file_cache")
    hit = cache.get_or_download("abcdefghijk", download_audio)
    assert hit is not None
    hit.audio_path.write_bytes(b"bad")
    assert cache.get_hit("abcdefghijk") is None
    assert "Ignoring invalid audio cache entry for abcdefghijk" in caplog.text
    restored = cache.get_or_download("abcdefghijk", download_audio)
    assert restored is not None
    assert restored.audio_path.read_bytes() == b"audio"


def test_incomplete_download_never_becomes_a_hit(tmp_path: Path) -> None:
    """A failed size check leaves neither published bytes nor temporary files."""

    def incomplete(directory: Path) -> CachedStreamInfo:
        hit = download_audio(directory)
        return CachedStreamInfo(hit.audio_path, {"filesize": 10})

    with pytest.raises(ValueError, match="size does not match"):
        FileCache(tmp_path).get_or_download("abcdefghijk", incomplete)
    assert list(tmp_path.iterdir()) == []


def test_invalid_metadata_is_a_miss(tmp_path: Path) -> None:
    """Non-object sidecars are rejected without an attribute error."""
    cache = FileCache(tmp_path)
    cache.get_or_download("abcdefghijk", download_audio)
    (tmp_path / "abcdefghijk" / "metadata.json").write_text(json.dumps([]), encoding="utf-8")
    assert cache.get_hit("abcdefghijk") is None
