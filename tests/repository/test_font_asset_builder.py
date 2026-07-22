from __future__ import annotations

import hashlib
import importlib.util
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_font_assets",
    ROOT / "firmware" / "apps" / "voice_terminal" / "tools" / "build_font_assets.py",
)
assert SPEC is not None and SPEC.loader is not None
FONT_BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FONT_BUILDER)


class FakeResponse:
    def __init__(self, payload: bytes, content_length: int | None) -> None:
        self.payload = payload
        self.headers = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )
        self.read_calls: list[int] = []

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.read_calls.append(size)
        return self.payload[:size]


@pytest.mark.parametrize("failure", ["size", "hash"])
def test_download_rejects_invalid_cached_package(
    tmp_path: Path,
    failure: str,
) -> None:
    destination = tmp_path / "font-package.zip"
    payload = b"cached-package"
    destination.write_bytes(payload)
    expected_size = len(payload) + (1 if failure == "size" else 0)
    expected_hash = hashlib.sha256(
        payload if failure == "size" else b"different-package"
    ).hexdigest()

    with pytest.raises(ValueError, match=f"package {'size' if failure == 'size' else 'SHA-256'} mismatch"):
        FONT_BUILDER.download(
            "https://invalid.example/font.zip",
            destination,
            expected_hash,
            expected_size,
            "package",
        )

    assert destination.read_bytes() == payload


def test_download_rejects_oversized_content_length_without_reading_or_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "font-package.zip"
    response = FakeResponse(b"oversized", content_length=9)
    monkeypatch.setattr(FONT_BUILDER.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="package Content-Length mismatch"):
        FONT_BUILDER.download(
            "https://invalid.example/font.zip",
            destination,
            hashlib.sha256(b"safe").hexdigest(),
            4,
            "package",
        )

    assert response.read_calls == []
    assert not destination.exists()
    assert not destination.with_suffix(".zip.tmp").exists()


def test_download_rejects_oversized_body_without_publishing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "font-package.zip"
    response = FakeResponse(b"oversized", content_length=None)
    monkeypatch.setattr(FONT_BUILDER.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="package download size mismatch"):
        FONT_BUILDER.download(
            "https://invalid.example/font.zip",
            destination,
            hashlib.sha256(b"safe").hexdigest(),
            4,
            "package",
        )

    assert response.read_calls == [5]
    assert not destination.exists()
    assert not destination.with_suffix(".zip.tmp").exists()


def test_package_rejects_wrong_entry_size_before_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "font-package.zip"
    package.touch()

    class FakeArchive:
        def __enter__(self) -> FakeArchive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def namelist(self) -> list[str]:
            return [f"package/cbin/{FONT_BUILDER.FONT_NAME}"]

        def getinfo(self, _name: str) -> SimpleNamespace:
            return SimpleNamespace(file_size=FONT_BUILDER.FONT_SIZE + 1)

        def read(self, _name: str) -> bytes:
            raise AssertionError("oversized ZIP entry must not be read")

    monkeypatch.setattr(FONT_BUILDER.zipfile, "ZipFile", lambda _path: FakeArchive())

    with pytest.raises(ValueError, match="Qwen CBIN size mismatch"):
        FONT_BUILDER.read_font_from_package(package)


def test_reads_pinned_font_from_synthetic_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    font = b"synthetic-cbin"
    font_name = "synthetic.bin"
    package = tmp_path / "font-package.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(f"fixture/cbin/{font_name}", font)

    monkeypatch.setattr(FONT_BUILDER, "FONT_NAME", font_name)
    monkeypatch.setattr(FONT_BUILDER, "FONT_SIZE", len(font))
    monkeypatch.setattr(FONT_BUILDER, "FONT_SHA256", hashlib.sha256(font).hexdigest())

    assert FONT_BUILDER.read_font_from_package(package) == font
