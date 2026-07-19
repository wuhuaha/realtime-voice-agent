from __future__ import annotations

import argparse
import re
from pathlib import Path

from verify_repository import tracked_files

PATTERNS = {
    "provider_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
ASSIGNED_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|token|password|secret)\s*[:=]\s*['\"]?(?P<value>[A-Za-z0-9/+_=-]{16,})"
)
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".example",
    ".h",
    ".hpp",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def scan(root: Path, paths: tuple[str, ...]) -> list[str]:
    findings: list[str] = []
    for raw_path in paths:
        path = root / raw_path
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{raw_path}:{line_number}: possible {kind}")
                    break
            else:
                match = ASSIGNED_SECRET.search(line)
                if match is None:
                    continue
                value = match.group("value")
                if value.startswith(("replace-", "validator-")):
                    continue
                if value == value.upper() and "_" in value:
                    continue
                findings.append(f"{raw_path}:{line_number}: possible assigned_secret")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan(root, tracked_files(root))
    if findings:
        for finding in findings:
            print(f"ERROR: {finding}")
        return 1
    print("Tracked secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
