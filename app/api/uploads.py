"""Приём загруженного контракта: временный файл удаляется сразу после обработки."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

ALLOWED_SUFFIXES = {".pdf", ".docx"}


def validate_upload(filename: str | None, content: bytes, max_bytes: int) -> str:
    if not content:
        raise HTTPException(status_code=400, detail="пустой файл")
    if len(content) > max_bytes:
        megabytes = max(1, (max_bytes + 1024 * 1024 - 1) // (1024 * 1024))
        raise HTTPException(status_code=413, detail=f"файл больше {megabytes} МБ")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="принимаются только файлы PDF и DOCX")
    return suffix


@contextmanager
def stored_upload(content: bytes, suffix: str, *, root: Path) -> Iterator[Path]:
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="complegge-", dir=root))
    path = directory / f"upload{suffix}"
    try:
        path.write_bytes(content)
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)
