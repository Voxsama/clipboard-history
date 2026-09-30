#!/usr/bin/env python3
"""Update Clipboard History from a GitHub repository.

Versions are GitHub *tags* (v2.1, v2.2, ...). The repository is set once with
    clipboard-history --set-update-repo YOUR-USERNAME/clipboard-history
and then
    clipboard-history --check-update     # is there a newer version?
    clipboard-history --update           # download, install, restart
    clipboard-history --rollback         # go back to the version before the last update

Safety: only https downloads from the configured repository, the new code is
compile-checked before anything is replaced, the previous version is kept in
<app dir>/previous, and if the new version does not start it is rolled back
automatically. History, pins and settings are never touched.

This module needs no GTK, so it works even when the app itself is broken.
"""
from __future__ import annotations

import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

APP_NAME = "clipboard-history"
APP_DIR = Path(__file__).resolve().parent
MAIN = APP_DIR / "clipboard_history.py"
PREVIOUS = APP_DIR / "previous"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME / "config.json"

# Overridable for testing only.
API = os.environ.get("CLIPBOARD_HISTORY_API", "https://api.github.com")
DOWNLOAD = os.environ.get("CLIPBOARD_HISTORY_DOWNLOAD", "https://codeload.github.com")

APP_FILES = ["clipboard_history.py", "emoji_data.py", "style.css", "updater.py"]
REQUIRED = ["clipboard_history.py", "emoji_data.py", "style.css"]
MAX_DOWNLOAD = 20 * 1024 * 1024
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(r"^v?(\d+(?:\.\d+){0,2})$")


class UpdateError(Exception):
    """A problem the user should read, not a crash."""


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    m = TAG_RE.match(text.strip())
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def version_in_file(path: Path) -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    if not m:
        raise UpdateError(f"Could not find the version number in {path.name}.")
    return m.group(1)


def fmt(v: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in v)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def configured_repo() -> str:
    repo = read_config().get("update_repo", "")
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        return ""
    return repo


def set_repo(repo: str) -> None:
    repo = repo.strip().removeprefix("https://github.com/").strip("/")
    repo = repo.removesuffix(".git")
    if not REPO_RE.match(repo):
        raise UpdateError('The repository must look like "your-username/clipboard-history".')
    data = read_config()
    data["update_repo"] = repo
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    print(f"Update source saved: {repo}")


def need_repo() -> str:
    repo = configured_repo()
    if not repo:
        raise UpdateError(
            "No update source is set yet. Run this once (use your own GitHub name):\n"
            "  clipboard-history --set-update-repo YOUR-USERNAME/clipboard-history")
    return repo


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
def http_get(url: str, limit: int = 2 * 1024 * 1024) -> bytes:
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise UpdateError("Refusing to download over an insecure connection.")
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-updater",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("That repository or version was not found. Is the repository public "
                              "and does it have a version tag like v2.1?") from e
        if e.code in (403, 429):
            raise UpdateError("GitHub is limiting requests right now. Try again in a few minutes.") from e
        raise UpdateError(f"GitHub answered with an error ({e.code}).") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise UpdateError("Could not reach GitHub. Check your internet connection.") from e
    if len(data) > limit:
        raise UpdateError("The download is unexpectedly large, so it was refused.")
    return data


def latest_tag(repo: str) -> tuple[str, tuple[int, int, int]]:
    raw = http_get(f"{API}/repos/{repo}/tags?per_page=100")
    try:
        tags = json.loads(raw)
    except ValueError as e:
        raise UpdateError("GitHub sent something unexpected.") from e
    best: Optional[tuple[tuple[int, int, int], str]] = None
    for t in tags if isinstance(tags, list) else []:
        name = t.get("name", "") if isinstance(t, dict) else ""
        ver = parse_version(name)
        if ver and (best is None or ver > best[0]):
            best = (ver, name)
    if best is None:
        raise UpdateError('No version tags found. On GitHub the repo needs a tag such as "v2.1".')
    return best[1], best[0]


def check(repo: Optional[str] = None) -> dict:
    repo = repo or need_repo()
    tag, ver = latest_tag(repo)
    cur = parse_version(version_in_file(MAIN)) or (0, 0, 0)
    return {"repo": repo, "tag": tag, "latest": ver, "current": cur, "newer": ver > cur}


# --------------------------------------------------------------------------- #
# Download + validate
# --------------------------------------------------------------------------- #
def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise UpdateError("The download contains unsafe file paths, so it was refused.")
        if m.isfile() or m.isdir():
            yield m  # links, devices etc. are skipped


def fetch_source(repo: str, tag: str, workdir: Path) -> Path:
    data = http_get(f"{DOWNLOAD}/{repo}/tar.gz/refs/tags/{tag}", MAX_DOWNLOAD)
    archive = workdir / "src.tar.gz"
    archive.write_bytes(data)
    dest = workdir / "src"
    dest.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = list(_safe_members(tar))
            try:
                tar.extractall(dest, members=members, filter="data")  # Python 3.12+
            except TypeError:
                tar.extractall(dest, members=members)
    except (tarfile.TarError, OSError) as e:
        raise UpdateError("The downloaded file could not be opened.") from e
    for root, dirs, files in os.walk(dest):
        dirs.sort()
        if all(f in files for f in REQUIRED):
            return Path(root)
    raise UpdateError("The download does not contain the app files "
                      "(clipboard_history.py, emoji_data.py, style.css).")


def validate(src: Path, expect: tuple[int, int, int]) -> None:
    with tempfile.TemporaryDirectory() as td:
        for f in src.glob("*.py"):
            try:
                py_compile.compile(str(f), cfile=str(Path(td) / (f.name + "c")), doraise=True)
            except py_compile.PyCompileError as e:
                raise UpdateError(f"The new version has a code error ({f.name}); nothing was changed.") from e
    new = parse_version(version_in_file(src / "clipboard_history.py"))
    if new != expect:
        raise UpdateError(
            f"The tag says {fmt(expect)} but the code inside says "
            f"{version_in_file(src / 'clipboard_history.py')}. "
            "Set __version__ in clipboard_history.py to match the tag, commit, and make a new tag. "
            "Nothing was changed.")


# --------------------------------------------------------------------------- #
# Install / restart / rollback
# --------------------------------------------------------------------------- #
def _copy_atomic(src: Path, dst: Path, mode: int) -> None:
    tmp = dst.with_name(dst.name + ".new")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dst)


def _install_files(src: Path) -> None:
    for name in APP_FILES:
        f = src / name
        if f.exists():
            _copy_atomic(f, APP_DIR / name, 0o755 if name.endswith("clipboard_history.py") else 0o644)


def _backup_current() -> None:
    if PREVIOUS.exists():
        shutil.rmtree(PREVIOUS)
    PREVIOUS.mkdir()
    for name in APP_FILES:
        f = APP_DIR / name
        if f.exists():
            shutil.copyfile(f, PREVIOUS / name)


def _run_main(*args: str) -> int:
    try:
        return subprocess.run([sys.executable, str(MAIN), *args], timeout=10,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def restart_service() -> bool:
    """Stop the running service and start the installed version. True if it came up."""
    if not _has_display():
        print("No desktop session detected; the new version starts at your next login.")
        return True
    _run_main("--quit")
    time.sleep(0.8)
    subprocess.Popen([sys.executable, str(MAIN), "--daemon"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.4)
        if _run_main("--status") == 0:
            return True
    return False


def rollback(quiet: bool = False) -> None:
    if not (PREVIOUS / "clipboard_history.py").exists():
        raise UpdateError("There is no previous version to go back to.")
    _install_files(PREVIOUS)
    ok = restart_service()
    if not quiet:
        print(f"Restored version {version_in_file(MAIN)}." + ("" if ok else " (The service did not start; run clipboard-history --toggle.)"))


def update(force: bool = False) -> None:
    info = check()
    if not info["newer"] and not force:
        print(f"Already up to date (version {fmt(info['current'])}).")
        return
    print(f"Updating {fmt(info['current'])} -> {fmt(info['latest'])} from {info['repo']} ...")
    with tempfile.TemporaryDirectory(prefix="ch-update-") as td:
        src = fetch_source(info["repo"], info["tag"], Path(td))
        validate(src, info["latest"])
        _backup_current()
        try:
            _install_files(src)
        except OSError as e:
            rollback(quiet=True)
            raise UpdateError("Could not write the new files; the old version was restored.") from e
    if restart_service():
        print(f"Done. Now running version {version_in_file(MAIN)}. Your history and pins are unchanged.")
    else:
        print("The new version did not start. Going back to the previous one ...")
        rollback(quiet=True)
        raise UpdateError("The update was undone because the new version failed to start.")


# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    try:
        if cmd == "set-repo":
            if len(argv) < 2:
                raise UpdateError('Usage: clipboard-history --set-update-repo YOUR-USERNAME/clipboard-history')
            set_repo(argv[1])
        elif cmd == "check":
            info = check()
            if info["newer"]:
                print(f"Update available: {fmt(info['current'])} -> {fmt(info['latest'])}. "
                      "Run: clipboard-history --update")
            else:
                print(f"You have the latest version ({fmt(info['current'])}).")
        elif cmd == "update":
            update(force="--force" in argv)
        elif cmd == "rollback":
            rollback()
        else:
            raise UpdateError(f"Unknown update command: {cmd}")
    except UpdateError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
