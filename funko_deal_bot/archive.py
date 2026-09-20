from __future__ import annotations

import zipfile
from pathlib import Path

ARCHIVE_NAME = "FunkoDealBot.zip"
OPERATOR_TELEGRAM_PROXY = ""
INCLUDE_FILES = (
    "README.md",
    "V22_STATS.md",
    "pyproject.toml",
    ".env.example",
    ".gitignore",
    "VERSION.txt",
    "START.bat",
    "START.sh",
    "uv.lock",
)
INCLUDE_DIRS = ("src", "scripts", "tests")
SKIP_DIR_NAMES = {
    ".venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "dist",
    "data",
}


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def archive_path(root: Path | None = None) -> Path:
    base = root or project_root()
    return base / "dist" / ARCHIVE_NAME


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.endswith(".egg-info")


def collect_source_files(root: Path | None = None) -> list[tuple[Path, str]]:
    """Return (absolute path, archive member path) pairs for a clean source zip."""
    root = (root or project_root()).resolve()
    items: list[tuple[Path, str]] = []
    prefix = "FunkoDealBot"

    for name in INCLUDE_FILES:
        path = root / name
        if path.is_file():
            items.append((path, f"{prefix}/{name}"))

    for dirname in INCLUDE_DIRS:
        folder = root / dirname
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_dir():
                continue
            rel_path = path.relative_to(root)
            if any(_should_skip_dir(part) for part in rel_path.parts):
                continue
            if path.suffix == ".sqlite" or path.name.endswith(".sqlite-wal"):
                continue
            rel = rel_path.as_posix()
            items.append((path, f"{prefix}/{rel}"))

    items.sort(key=lambda pair: pair[1])
    return items


def _windows_bat_bytes(data: bytes) -> bytes:
    """cmd.exe: CRLF, no UTF-8 BOM. A BOM makes `@echo off` run as `я╗┐@echo`."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")


def _zip_write(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    if path.suffix.lower() == ".bat":
        zf.writestr(arcname, _windows_bat_bytes(path.read_bytes()))
        return
    zf.write(path, arcname)


def write_source_archive(dest: Path | None = None, root: Path | None = None) -> Path:
    root = (root or project_root()).resolve()
    dest = dest or archive_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    files = collect_source_files(root)
    tmp = dest.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            _zip_write(zf, path, arcname)
    tmp.replace(dest)
    return dest


def operator_env_text(raw: str) -> str:
    """Keep the operator eBay PROXY_URL; never force a machine-local Telegram proxy on remote servers."""
    lines: list[str] = []
    has_proxy = False
    has_tg = False
    for line in raw.splitlines():
        stripped = line.lstrip()
        # Never bake old HTTP proxy leftovers (even as comments).
        if "PROXY_URL=" in stripped and "http://" in stripped and stripped.startswith("#"):
            continue
        if stripped.startswith("PROXY_URL="):
            # eBay proxy is an operator setting and must survive packaging.
            # Do not print or rewrite the value; it may contain credentials.
            lines.append(line)
            has_proxy = True
            continue
        if stripped.startswith("TELEGRAM_PROXY="):
            lines.append(f"TELEGRAM_PROXY={OPERATOR_TELEGRAM_PROXY}")
            has_tg = True
            continue
        lines.append(line)
    if not has_proxy:
        lines.append("PROXY_URL=")
    if not has_tg:
        lines.append(f"TELEGRAM_PROXY={OPERATOR_TELEGRAM_PROXY}")
    return "\n".join(lines).rstrip() + "\n"


def write_full_archive(
    dest: Path | None = None,
    root: Path | None = None,
    *,
    env_path: Path | None = None,
) -> Path:
    """Source zip plus operator `.env` (including operator eBay proxy). Never git-commit `.env`."""
    root = (root or project_root()).resolve()
    dest = dest or archive_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env_file = (env_path or (root / ".env")).resolve()
    files = collect_source_files(root)
    tmp = dest.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            _zip_write(zf, path, arcname)
        if env_file.is_file():
            baked = operator_env_text(env_file.read_text(encoding="utf-8"))
            zf.writestr("FunkoDealBot/.env", baked)
    tmp.replace(dest)
    return dest


def ensure_source_archive(dest: Path | None = None, root: Path | None = None) -> Path:
    dest = dest or archive_path(root)
    if not dest.is_file():
        return write_source_archive(dest=dest, root=root)
    return dest
