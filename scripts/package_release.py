"""Package a source allowlist, never environment files, data, or personal docs."""

import hashlib
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = [
    "pyproject.toml",
    "README.md",
    "PROJECT_PLAN.md",
    "requirements.lock",
    "Dockerfile",
    "compose.yaml",
    ".env.example",
    ".env.server.example",
    ".dockerignore",
]
DIRECTORIES = ["contentpilot_agent", "deployment", "scripts", "docs", "tests", ".github/workflows"]
EXTENSIONS = {".py", ".js", ".css", ".html", ".md", ".sh", ".example", ".yml", ".yaml"}


def release_files():
    paths = [ROOT / name for name in TOP]
    for folder in DIRECTORIES:
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in EXTENSIONS and "__pycache__" not in path.parts:
                paths.append(path)
    return sorted(paths)


def main():
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    archive = out / "contentpilot-0.7.0.tar.gz"
    files = release_files()
    for path in files:
        if path.is_symlink() or not path.is_file() or ROOT not in path.resolve().parents:
            raise RuntimeError(f"Unexpected release path: {path.name}")
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in files:
            entry = tar.gettarinfo(str(path), arcname=path.relative_to(ROOT).as_posix())
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            entry.mode = 0o755 if path.suffix == ".sh" else 0o644
            with path.open("rb") as source:
                tar.addfile(entry, source)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / (archive.name + ".sha256")).write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"{archive}\n{len(files)} source files; SHA256 {digest}")


if __name__ == "__main__":
    main()
