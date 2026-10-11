"""Capture tested dependencies without installing or accessing credentials.

Use within the project venv after a deliberate dependency upgrade. Windows-only
packages retain platform markers so the constraints also resolve on Linux.
"""

from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {"contentpilot-agent", "mcp-types", "httpx2", "httpcore2"}


def main():
    entries = []
    for dist in distributions():
        name = dist.metadata["Name"].lower().replace("_", "-")
        if name in EXCLUDE or name in {"pip", "setuptools", "wheel"}:
            continue
        marker = '; sys_platform == "win32"' if name in {"pywin32", "colorama"} else ""
        entries.append(f"{name}=={dist.version}{marker}")
    header = "# Tested constraints (Python 3.13); refresh deliberately using scripts/lock_dependencies.py.\n"
    (ROOT / "requirements.lock").write_text(
        header + "\n".join(sorted(set(entries))) + "\n", encoding="utf-8"
    )
    print(f"Locked {len(entries)} dependencies")


if __name__ == "__main__":
    main()
