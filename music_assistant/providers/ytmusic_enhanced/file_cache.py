"""Persistent audio cache; blocking operations are serialized within each instance."""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


@dataclass(frozen=True)
class CachedStreamInfo:
    """A complete audio file and the format information needed for playback."""

    audio_path: Path
    metadata: dict[str, Any]


class FileCache:
    """Own only video-ID directories under an instance-specific cache root."""

    def __init__(
        self, cache_dir: Path, max_size_bytes: int = 0, *, logger: logging.Logger | None = None
    ) -> None:
        """Set the private cache directory and retained-audio size limit."""
        if max_size_bytes < 0:
            raise ValueError("Cache size cannot be negative")
        self.cache_dir = cache_dir
        self.max_size_bytes = max_size_bytes
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._lock = threading.RLock()

    def ensure_ready(self) -> None:
        """Create the cache root and enforce the configured limit."""
        with self._lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._evict()

    def get_hit(self, video_id: str) -> CachedStreamInfo | None:
        """Return a validated complete file, updating its last-access time."""
        with self._lock:
            entry = self._entry(video_id)
            if entry.is_symlink():
                self._logger.debug("Ignoring symbolic-link audio cache entry for %s", video_id)
                return None
            sidecar = entry / "metadata.json"
            if not sidecar.is_file() or sidecar.is_symlink():
                return None
            try:
                metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                audio = entry / "audio"
                if (
                    not isinstance(metadata, dict)
                    or audio.is_symlink()
                    or not audio.is_file()
                    or audio.stat().st_size <= 0
                    or audio.stat().st_size != metadata.get("size")
                ):
                    self._logger.debug("Ignoring invalid audio cache entry for %s", video_id)
                    return None
                sidecar.touch()
            except (OSError, ValueError) as err:
                self._logger.debug(
                    "Cannot read audio cache entry for %s (%s)", video_id, type(err).__name__
                )
                return None
            return CachedStreamInfo(audio, metadata)

    def get_or_download(
        self,
        video_id: str,
        download: Callable[[Path], CachedStreamInfo],
    ) -> CachedStreamInfo | None:
        """Publish completed downloads atomically; oversized files are not retained."""
        with self._lock:
            if hit := self.get_hit(video_id):
                return hit
            entry = self._entry(video_id)
            if entry.is_symlink():
                raise ValueError("Cache entries must not be symbolic links")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".download-", dir=self.cache_dir) as work:
                work_dir = Path(work)
                downloaded = download(work_dir)
                audio = downloaded.audio_path.resolve()
                if not audio.is_relative_to(work_dir.resolve()) or not audio.is_file():
                    raise ValueError("Download escaped its temporary directory")
                size = audio.stat().st_size
                if not size:
                    raise ValueError("Download produced an empty audio file")
                expected_size = downloaded.metadata.get("filesize")
                if expected_size is not None and size != expected_size:
                    raise ValueError("Download size does not match the selected audio format")
                if self.max_size_bytes and size > self.max_size_bytes:
                    self._logger.debug(
                        "Discarding oversized audio for %s: %s bytes exceed the %s byte limit",
                        video_id,
                        size,
                        self.max_size_bytes,
                    )
                    return None
                metadata = {**downloaded.metadata, "size": size}
                staged = work_dir / "complete"
                staged.mkdir()
                audio.replace(staged / "audio")
                (staged / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                if entry.exists():
                    shutil.rmtree(entry)
                self._evict(incoming_size=size)
                staged.replace(entry)
            return CachedStreamInfo(entry / "audio", metadata)

    def _entry(self, video_id: str) -> Path:
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError("Invalid YouTube video ID")
        return self.cache_dir / video_id

    def _evict(self, incoming_size: int = 0) -> None:
        if not self.max_size_bytes:
            return
        entries: list[tuple[int, Path, int]] = []
        for entry in self.cache_dir.iterdir():
            if not VIDEO_ID.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            audio = entry / "audio"
            sidecar = entry / "metadata.json"
            if audio.is_symlink() or sidecar.is_symlink():
                continue
            if audio.is_file() and sidecar.is_file():
                entries.append((sidecar.stat().st_mtime_ns, entry, audio.stat().st_size))
        total_size = sum(size for _, _, size in entries) + incoming_size
        for _, entry, size in sorted(entries):
            if total_size <= self.max_size_bytes:
                return
            shutil.rmtree(entry)
            total_size -= size
            self._logger.debug(
                "Evicted audio cache entry %s (%s bytes); retained audio=%s bytes, limit=%s bytes",
                entry.name,
                size,
                total_size - incoming_size,
                self.max_size_bytes,
            )
