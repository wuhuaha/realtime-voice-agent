"""Build the versioned, reproducible full-Chinese font-assets image."""

from __future__ import annotations

import argparse
import hashlib
import struct
import urllib.request
import zipfile
from pathlib import Path

FONT_PACKAGE_URL = (
    "https://components-file.espressif.com/components/78/xiaozhi-fonts/1.6.0/"
    "78__xiaozhi-fonts-v1.6.0.zip"
)
FONT_PACKAGE_NAME = "78__xiaozhi-fonts-v1.6.0.zip"
FONT_PACKAGE_SHA256 = "255868d6e225d08038f38add8f7f2bf2e3567ef7a3b0edcd9703d2101f56e7d5"
FONT_PACKAGE_SIZE = 31_346_713
FONT_NAME = "font_noto_qwen_20_4.bin"
FONT_SHA256 = "601422de3a49c05265ed853c8054b73b532729e667a6d63f34bb72eab1935345"
FONT_SIZE = 2_998_916
SOURCE_ID = b"qwen20-4-v1.6.0"
MAGIC = b"RVAFNT1\0"
FORMAT_VERSION = 1
HEADER = struct.Struct("<8sHHI32s16s")
PARTITION_SIZE = 8 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_file(
    path: Path, expected_sha256: str, label: str, expected_size: int | None = None
) -> Path:
    if expected_size is not None and path.stat().st_size != expected_size:
        raise ValueError(f"{label} size mismatch")
    actual = sha256(path.read_bytes())
    if actual != expected_sha256:
        raise ValueError(f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual}")
    return path


def download(
    url: str,
    destination: Path,
    expected_sha256: str,
    expected_size: int,
    label: str,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return checked_file(destination, expected_sha256, label, expected_size)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        received = temporary.stat().st_size if temporary.exists() else 0
        for _attempt in range(8):
            if received >= expected_size:
                break
            headers = {"User-Agent": "rva-font-builder/1"}
            mode = "wb"
            if received > 0:
                headers["Range"] = f"bytes={received}-"
                mode = "ab"
            request = urllib.request.Request(url, headers=headers)
            before = received
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open(mode) as output:
                content_length = response.headers.get("Content-Length")
                if received == 0 and content_length is not None and int(content_length) != expected_size:
                    raise ValueError(f"{label} Content-Length mismatch")
                while True:
                    # Read at most one byte beyond the declared size so an
                    # untrusted response cannot force an oversized allocation.
                    remaining = expected_size - received
                    chunk = response.read(min(1024 * 1024, remaining + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected_size:
                        raise ValueError(
                            f"{label} download size mismatch: expected {expected_size}, got at least {received}"
                        )
                    output.write(chunk)
            if received == before:
                break
            if received != expected_size:
                continue
        if received != expected_size:
            raise ValueError(f"{label} download size mismatch: expected {expected_size}, got {received}")
        checked_file(temporary, expected_sha256, label, expected_size)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_font_from_package(package: Path) -> bytes:
    with zipfile.ZipFile(package) as archive:
        matches = [
            name for name in archive.namelist()
            if Path(name).name == FONT_NAME and Path(name).parent.name == "cbin"
        ]
        if len(matches) != 1:
            raise ValueError(f"font package contains {len(matches)} matching CBIN files")
        if archive.getinfo(matches[0]).file_size != FONT_SIZE:
            raise ValueError("Qwen CBIN size mismatch")
        font = archive.read(matches[0])
    if sha256(font) != FONT_SHA256:
        raise ValueError("Qwen CBIN SHA-256 mismatch")
    return font


def build_image(cbin: bytes) -> bytes:
    digest = hashlib.sha256(cbin).digest()
    header = HEADER.pack(
        MAGIC,
        FORMAT_VERSION,
        HEADER.size,
        len(cbin),
        digest,
        SOURCE_ID.ljust(16, b"\0"),
    )
    image = header + cbin
    if len(image) > PARTITION_SIZE:
        raise ValueError(f"font assets exceed {PARTITION_SIZE} bytes: {len(image)}")
    return image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--font-cbin", type=Path, help="use the pinned Qwen CBIN from this path")
    parser.add_argument(
        "--font-package", type=Path, help="use the pinned xiaozhi-fonts package archive"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.font_cbin is not None:
        cbin = checked_file(
            args.font_cbin, FONT_SHA256, "Qwen CBIN", FONT_SIZE
        ).read_bytes()
    else:
        package = args.font_package or download(
            FONT_PACKAGE_URL,
            args.cache_dir / FONT_PACKAGE_NAME,
            FONT_PACKAGE_SHA256,
            FONT_PACKAGE_SIZE,
            "xiaozhi-fonts package",
        )
        checked_file(
            package,
            FONT_PACKAGE_SHA256,
            "xiaozhi-fonts package",
            FONT_PACKAGE_SIZE,
        )
        cbin = read_font_from_package(package)
    image = build_image(cbin)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    print(
        f"generated {args.output} ({len(image)} bytes, sha256={sha256(image)}, "
        f"source={SOURCE_ID.decode()}, font={FONT_NAME})"
    )


if __name__ == "__main__":
    main()
