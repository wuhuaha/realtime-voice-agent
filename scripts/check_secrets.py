from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "provider_api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
SECRET_NAME = r"(?:api[_-]?key|token|password|secret)"
QUOTED_SECRET = re.compile(
    rf"(?i){SECRET_NAME}\s*[:=]\s*(?P<quote>['\"])(?P<value>[^'\"]{{16,}})(?P=quote)\s*[,;]?\s*$"
)
SCALAR_SECRET = re.compile(
    rf"(?i)^\s*{SECRET_NAME}\s*[:=]\s*(?P<value>[A-Za-z0-9/+_=-]{{16,}})\s*(?:#.*)?$"
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
                match = QUOTED_SECRET.search(line) or SCALAR_SECRET.search(line)
                if match is None:
                    continue
                value = match.group("value")
                if value.startswith(("replace-", "validator-")):
                    continue
                if value == value.upper() and "_" in value:
                    continue
                findings.append(f"{raw_path}:{line_number}: possible assigned_secret")
    return findings


def repository_files(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan(root, repository_files(root))
    if findings:
        for finding in findings:
            print(f"ERROR: {finding}")
        return 1
    print("Tracked and untracked secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
