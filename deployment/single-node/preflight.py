from __future__ import annotations

import os
import sys

PLACEHOLDER_MARKER = "replace-with-"


def placeholder_environment_names(environment: dict[str, str]) -> list[str]:
    """Return application variables that still contain repository placeholders."""
    return sorted(
        name
        for name, value in environment.items()
        if name.startswith("VOICE_") and PLACEHOLDER_MARKER in value.lower()
    )


def main(argv: list[str]) -> int:
    invalid = placeholder_environment_names(dict(os.environ))
    if invalid:
        print(
            "refusing to start: replace repository placeholders in " + ", ".join(invalid),
            file=sys.stderr,
        )
        return 78
    if not argv:
        print("refusing to start: no application command was provided", file=sys.stderr)
        return 64
    os.execvp(argv[0], argv)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
