"""Build the versioned, reproducible full-Chinese font-assets image."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import subprocess
import tarfile
import urllib.request
from pathlib import Path

FONT_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/"
    "Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf"
)
FONT_NAME = "NotoSansCJKsc-Regular-Sans2.004.otf"
FONT_SHA256 = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
CONVERTER_URL = "https://registry.npmjs.org/lv_font_conv/-/lv_font_conv-1.5.3.tgz"
CONVERTER_NAME = "lv_font_conv-1.5.3.tgz"
CONVERTER_SHA256 = "9f64fb8eb553dbab1990402eae74afbafd80b4f39a8314a01484083b6ed1000d"
SOURCE_ID = b"notocjk-s2.004"
MAGIC = b"RVAFNT1\0"
FORMAT_VERSION = 1
HEADER = struct.Struct("<8sHHI32s16s")
PARTITION_SIZE = 8 * 1024 * 1024
FONT_RANGES = (
    "0x20-0x7E,0x2000-0x206F,0x2E80-0x2EFF,0x2F00-0x2FDF,"
    "0x3000-0x303F,0x31C0-0x31EF,0x3400-0x4DBF,0x4E00-0x9FFF,"
    "0xF900-0xFAFF,0xFE10-0xFE1F,0xFE30-0xFE4F,0xFF00-0xFFEF"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_file(path: Path, expected_sha256: str, label: str) -> Path:
    actual = sha256(path.read_bytes())
    if actual != expected_sha256:
        raise ValueError(f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual}")
    return path


def download(url: str, destination: Path, expected_sha256: str, label: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return checked_file(destination, expected_sha256, label)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "rva-font-builder/1"})
        with urllib.request.urlopen(request, timeout=180) as response:
            temporary.write_bytes(response.read())
        checked_file(temporary, expected_sha256, label)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def extract_converter(archive: Path, cache_dir: Path) -> Path:
    destination = cache_dir / "lv_font_conv-1.5.3"
    entrypoint = destination / "package" / "lv_font_conv.js"
    if entrypoint.is_file():
        return entrypoint
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            root = temporary.resolve()
            for member in bundle.getmembers():
                target = (temporary / member.name).resolve()
                if root != target and root not in target.parents:
                    raise ValueError(f"converter archive contains unsafe path: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise ValueError(f"converter archive contains unsupported entry: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise ValueError(f"converter archive entry cannot be read: {member.name}")
                    with source, target.open("wb") as destination_file:
                        shutil.copyfileobj(source, destination_file)
                else:
                    raise ValueError(f"converter archive contains unsupported entry: {member.name}")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    if not entrypoint.is_file():
        raise ValueError("converter archive is missing package/lv_font_conv.js")
    return entrypoint


def build_cbin(font: Path, converter: Path, output: Path) -> bytes:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required for the pinned lv_font_conv tool")
    temporary = output.with_suffix(".cbin.tmp")
    temporary.unlink(missing_ok=True)
    command = [
        node,
        str(converter),
        "--size",
        "16",
        "--bpp",
        "2",
        "--format",
        "bin",
        "--font",
        str(font),
        "--range",
        FONT_RANGES,
        "--no-kerning",
        "--output",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True)
        cbin = temporary.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    if len(cbin) < 8 or cbin[4:8] != b"head":
        raise ValueError("converter output is not an LVGL binary font")
    first_table_size = struct.unpack_from("<I", cbin)[0]
    if first_table_size < 8 or first_table_size > len(cbin):
        raise ValueError("converter output contains an invalid head table size")
    return cbin


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
    parser.add_argument("--font", type=Path, help="use the pinned Noto source from this path")
    parser.add_argument(
        "--converter-archive", type=Path, help="use the pinned lv_font_conv npm archive"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.font is None:
        font = download(FONT_URL, args.cache_dir / FONT_NAME, FONT_SHA256, "font source")
    else:
        font = checked_file(args.font, FONT_SHA256, "font source")
    if args.converter_archive is None:
        archive = download(
            CONVERTER_URL,
            args.cache_dir / CONVERTER_NAME,
            CONVERTER_SHA256,
            "font converter",
        )
    else:
        archive = checked_file(args.converter_archive, CONVERTER_SHA256, "font converter")
    converter = extract_converter(archive, args.cache_dir)
    image = build_image(build_cbin(font, converter, args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    print(
        f"generated {args.output} ({len(image)} bytes, sha256={sha256(image)}, "
        f"source={SOURCE_ID.decode()}, converter=lv_font_conv@1.5.3)"
    )


if __name__ == "__main__":
    main()
