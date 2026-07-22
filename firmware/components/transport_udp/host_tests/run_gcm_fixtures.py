from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
COMPONENT = HERE.parent
FIXTURES = REPO / "protocol" / "udp_opus_gcm_v1" / "fixtures"


def run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    idf_path_value = os.environ.get("IDF_PATH")
    if not idf_path_value:
        raise RuntimeError("IDF_PATH is required to build the pinned Mbed TLS implementation")
    idf_path = Path(idf_path_value)
    mbedtls = idf_path / "components" / "mbedtls" / "mbedtls"
    compiler = shutil.which("g++")
    cmake = shutil.which("cmake")
    ninja = shutil.which("ninja")
    if not compiler or not cmake or not ninja:
        raise RuntimeError("g++, cmake and ninja are required")

    with tempfile.TemporaryDirectory(prefix="rva-gcm-fixture-") as temporary:
        root = Path(temporary)
        build = root / "mbedtls"
        run([
            cmake,
            "-S", str(mbedtls),
            "-B", str(build),
            "-G", "Ninja",
            "-DENABLE_PROGRAMS=OFF",
            "-DENABLE_TESTING=OFF",
        ])
        run([cmake, "--build", str(build), "--target", "mbedcrypto"])
        library = build / "library" / "libmbedcrypto.a"
        executable = root / ("gcm_fixture_runner.exe" if os.name == "nt" else "gcm_fixture_runner")
        run([
            compiler,
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{COMPONENT / 'include'}",
            f"-I{REPO / 'firmware/components/voice_contracts/include'}",
            f"-I{mbedtls / 'include'}",
            str(HERE / "gcm_fixture_runner.cc"),
            str(COMPONENT / "gcm_crypto.cc"),
            str(REPO / "firmware/components/voice_contracts/udp_wire.cc"),
            str(library),
            "-o", str(executable),
        ])

        positive = json.loads((FIXTURES / "positive.json").read_text(encoding="utf-8"))
        for vector in positive["vectors"]:
            run([
                str(executable), "positive", vector["direction"], vector["key_hex"],
                vector["salt_hex"], vector["header_hex"], vector["payload_hex"],
                vector["ciphertext_and_tag_hex"], vector["nonce_hex"],
            ])

        negative = json.loads((FIXTURES / "negative.json").read_text(encoding="utf-8"))
        for vector in negative["vectors"]:
            run([
                str(executable), "negative", negative["key_hex"], negative["salt_hex"],
                vector["datagram_hex"], vector["reject_stage"], "uplink",
            ])
    print("GCM fixtures: all positive and negative vectors passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
