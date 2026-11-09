"""Export a reviewable source handoff, without the workstation's runtime data."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import zipfile

ROOT_FILES = {
    "pyproject.toml", "README.md", "NOTICE.md", "DESIGN.md",
    "PRODUCT.md", "alembic.ini", ".gitignore", ".env.example",
    "install-windows.cmd", "start-windows.cmd",
}
SOURCE_DIRS = {"src", "scripts", "migrations", "tests", "config", "docs"}
EXCLUDED_DIRS = {"__pycache__", ".git", ".venv", ".pytest_cache", "data", "secrets", "logs", "cache", "node_modules"}
SOURCE_SUFFIXES = {".py", ".ps1", ".sh", ".cmd", ".html", ".css", ".js", ".json", ".md", ".mako"}
EXCLUDED_NAMES = {"session-secret.txt", ".session-secret", "thermal-status.json", "worker-runtime.json", "web-runtime.json"}


def source_files(root: Path):
    root = root.resolve()
    for name in sorted(ROOT_FILES):
        path = root / name
        if path.is_file() and not path.is_symlink():
            yield path
    for dirname in sorted(SOURCE_DIRS):
        directory = root / dirname
        if not directory.is_dir() or directory.is_symlink() or directory.resolve() != directory:
            continue
        for current, directories, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name for name in directories
                if name not in EXCLUDED_DIRS and not name.startswith(".")
                and not (current_path / name).is_symlink()
                and (current_path / name).resolve() == current_path / name
            )
            for name in sorted(files):
                path = current_path / name
                if (name in EXCLUDED_NAMES or name.startswith(".")
                        or path.suffix.lower() not in SOURCE_SUFFIXES or path.is_symlink()
                        or path.resolve() != path):
                    continue
                yield path


def export_source(root: Path, destination: Path) -> int:
    root = root.resolve()
    required = ("pyproject.toml", "alembic.ini", "README.md", "src/video_workstation/main.py",
                "scripts/setup.ps1", "scripts/start_portable_web.ps1")
    for name in required:
        if not (root / name).is_file():
            raise ValueError(f"Missing source file: {name}")
    files = list(source_files(root))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create: a previous handoff cannot be silently overwritten.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, Path("AI-Video-Workstation") / path.relative_to(root))
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New .zip path (must not exist)")
    args = parser.parse_args()
    count = export_source(Path(__file__).resolve().parents[1], args.destination.resolve())
    print(f"Exported {count} source files to {args.destination}")
