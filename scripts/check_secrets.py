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
HTML_SECRET_ATTRIBUTE = re.compile(
    rf"(?i)(?:data-)?{SECRET_NAME}\s*=\s*(?P<quote>['\"])(?P<value>[^'\"]{{16,}})(?P=quote)"
)
CMAKE_SECRET = re.compile(
    rf"(?i)\bset\s*\(\s*(?:[A-Za-z0-9_]*{SECRET_NAME}[A-Za-z0-9_]*)\s+"
    rf"(?P<quote>['\"])(?P<value>[^'\"]{{16,}})(?P=quote)\s*\)"
)
KCONFIG_SECRET = re.compile(rf"(?i)^\s*config\s+[A-Za-z0-9_]*{SECRET_NAME}[A-Za-z0-9_]*\s*$")
KCONFIG_DEFAULT = re.compile(
    r"^\s*default\s+(?P<quote>['\"])(?P<value>[^'\"]{16,})(?P=quote)(?:\s+if\s+.+)?\s*$"
)
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".example",
    ".defaults",
    ".h",
    ".html",
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
TEXT_FILENAMES = {"CMakeLists.txt", "Kconfig"}


def scan(root: Path, paths: tuple[str, ...]) -> list[str]:
    findings: list[str] = []
    for raw_path in paths:
        path = root / raw_path
        if (
            path.suffix.lower() not in TEXT_SUFFIXES
            and path.name not in TEXT_FILENAMES
        ) or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        kconfig_secret = False
        for line_number, line in enumerate(content.splitlines(), start=1):
            if path.name == "Kconfig" and line.lstrip().startswith("config "):
                kconfig_secret = KCONFIG_SECRET.match(line) is not None
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{raw_path}:{line_number}: possible {kind}")
                    break
            else:
                match = (
                    QUOTED_SECRET.search(line)
                    or SCALAR_SECRET.search(line)
                    or (HTML_SECRET_ATTRIBUTE.search(line) if path.suffix.lower() == ".html" else None)
                    or (CMAKE_SECRET.search(line) if path.name == "CMakeLists.txt" else None)
                    or (KCONFIG_DEFAULT.match(line) if kconfig_secret else None)
                )
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
