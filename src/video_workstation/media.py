from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Settings
from .models import Asset


class InvalidRange(ValueError):
    """Raised when an HTTP byte range is malformed or cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_range_header(value: str | None, file_size: int) -> ByteRange | None:
    if value is None:
        return None
    if file_size <= 0 or not value.startswith("bytes="):
        raise InvalidRange("无效的视频读取范围")

    specification = value.removeprefix("bytes=").strip()
    if not specification or "," in specification or "-" not in specification:
        raise InvalidRange("仅支持单段视频读取")

    start_text, end_text = specification.split("-", 1)
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise InvalidRange("无效的视频读取范围")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = file_size - 1 if not end_text else int(end_text)
    except ValueError as exc:
        raise InvalidRange("无效的视频读取范围") from exc

    if start < 0 or start >= file_size or end < start:
        raise InvalidRange("视频读取范围无法满足")
    return ByteRange(start=start, end=min(end, file_size - 1))


def resolve_asset_path(asset: Asset, settings: Settings) -> Path:
    try:
        path = Path(asset.path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("素材文件不存在") from exc
    if not path.is_file():
        raise ValueError("素材文件不存在")

    roots = (settings.asset_dir.resolve(), settings.archive_dir.resolve())
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("素材路径越界")
    return path


def iter_file_range(path: Path, byte_range: ByteRange, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    remaining = byte_range.length
    with path.open("rb") as stream:
        stream.seek(byte_range.start)
        while remaining:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
