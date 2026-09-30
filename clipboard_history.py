#!/usr/bin/env python3
"""Clipboard History - a Windows 11 (Win+V) style clipboard manager for Linux.

Single-instance background service + popup window (GTK 3 / PyGObject).

    clipboard-history              start the background service (no popup)
    clipboard-history --toggle     show/hide the popup (starts the service if needed)
    clipboard-history --show | --hide | --quit | --status

Wayland note: applications cannot grab global keys on Wayland, so the popup is
opened by binding a desktop shortcut to `clipboard-history --toggle`; that
command talks to the running instance over a Unix socket in $XDG_RUNTIME_DIR.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

__version__ = "2.0.0"
APP_NAME = "clipboard-history"

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
DATA_DIR = Path(os.environ.get("CLIPBOARD_HISTORY_DATA") or Path.home() / ".local" / "share" / APP_NAME)
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME
STATE_PATH = DATA_DIR / "state.json"   # remembered popup positions (per monitor)
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = DATA_DIR / "app.log"
APP_DIR = Path(__file__).resolve().parent  # style.css / emoji_data.py live next to this file


def runtime_dir() -> Path:
    """Directory for the socket + lock file ($XDG_RUNTIME_DIR, else a private /tmp dir)."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base)
    fallback = Path(f"/tmp/{APP_NAME}-{os.getuid()}")
    fallback.mkdir(mode=0o700, exist_ok=True)
    return fallback


SOCKET_PATH = runtime_dir() / f"{APP_NAME}.sock"
LOCK_PATH = runtime_dir() / f"{APP_NAME}.lock"

# --------------------------------------------------------------------------- #
# Command line + "client" mode. This runs BEFORE importing GTK so that
# `clipboard-history --toggle` against a running instance is nearly instant.
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Windows 11 style clipboard history. Bind `clipboard-history --toggle` to Super+V.",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--toggle", dest="command", action="store_const", const="toggle",
                       help="show or hide the popup (starts the service if not running)")
    group.add_argument("--show", dest="command", action="store_const", const="show", help="show the popup")
    group.add_argument("--hide", dest="command", action="store_const", const="hide", help="hide the popup")
    group.add_argument("--quit", dest="command", action="store_const", const="quit", help="stop the running service")
    group.add_argument("--status", dest="command", action="store_const", const="status",
                       help="print 'ok' if the service is running")
    p.add_argument("--check-update", action="store_true", help="check GitHub for a newer version")
    p.add_argument("--update", action="store_true", help="download and install the newest version")
    p.add_argument("--rollback", action="store_true", help="go back to the version before the last update")
    p.add_argument("--set-update-repo", metavar="USER/REPO", help="set the GitHub repository updates come from")
    p.add_argument("--daemon", action="store_true", help="run the background service without showing the popup (default)")
    p.add_argument("--debug", action="store_true", help="verbose logging to stderr")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def send_command(cmd: str, timeout: float = 1.5) -> Optional[str]:
    """Send one command to the running instance. Returns its reply or None if unreachable."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCKET_PATH))
        s.sendall((cmd + "\n").encode())
        data = s.recv(256)
        return data.decode(errors="replace").strip() or "ok"
    except (OSError, socket.timeout):
        return None
    finally:
        s.close()


ARGS: argparse.Namespace = parse_args(sys.argv[1:]) if __name__ == "__main__" else argparse.Namespace(
    command=None, daemon=False, debug=False)

if __name__ == "__main__" and (ARGS.check_update or ARGS.update or ARGS.rollback or ARGS.set_update_repo):
    sys.path.insert(0, str(APP_DIR))
    import updater  # no GTK needed

    if ARGS.set_update_repo:
        sys.exit(updater.main(["set-repo", ARGS.set_update_repo]))
    sys.exit(updater.main(["update" if ARGS.update else "rollback" if ARGS.rollback else "check"]))

if __name__ == "__main__" and ARGS.command:
    _reply = send_command(ARGS.command)
    if _reply is not None:  # a running instance handled it
        if ARGS.command == "status" or _reply not in ("ok",):
            print(_reply)
        sys.exit(0)
    if ARGS.command in ("hide", "quit", "status"):
        print("not running")
        sys.exit(1 if ARGS.command == "status" else 0)
    # toggle / show with no instance: fall through and start the service, then show.

# --------------------------------------------------------------------------- #
# GTK imports. GDK_BACKEND=x11 MUST be set before Gtk is imported: on GNOME
# Wayland, native Wayland clients cannot read the clipboard in the background,
# but XWayland clients can (Mutter syncs the selections). It also gives us
# window positioning, which Wayland does not allow.
# --------------------------------------------------------------------------- #
if os.environ.get("DISPLAY") or not os.environ.get("WAYLAND_DISPLAY"):
    os.environ["GDK_BACKEND"] = "x11"

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    try:
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11  # type: ignore
    except (ValueError, ImportError):
        GdkX11 = None  # type: ignore
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango
except (ImportError, ValueError) as exc:  # pragma: no cover
    sys.stderr.write(
        f"clipboard-history: PyGObject/GTK 3 is missing ({exc}).\n"
        "Install with: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0\n")
    sys.exit(1)

sys.path.insert(0, str(APP_DIR))
from emoji_data import EMOJI_CATEGORIES, parse_entry  # noqa: E402

# X11 WM_CLASS == "clipboard-history" so `for_window [class="clipboard-history"] floating enable` works.
GLib.set_prgname(APP_NAME)
GLib.set_application_name("Clipboard History")
Gdk.set_program_class(APP_NAME)

log = logging.getLogger(APP_NAME)

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
FRAME_W, FRAME_H = 360, 480      # visible panel size
SHADOW_MARGIN = 18               # transparent margin around the panel for the drop shadow
THUMB_MAX_W, THUMB_MAX_H = 270, 120
PREVIEW_CHARS = 600              # only the preview is truncated; the full text is stored
TEXT_TARGETS = {"UTF8_STRING", "STRING", "TEXT", "COMPOUND_TEXT", "text/plain",
                "text/plain;charset=utf-8", "text/plain;charset=UTF-8"}


def guarded(default: Any = None) -> Callable:
    """Decorator: log any exception instead of letting it escape a GTK callback."""

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> Any:
            try:
                return fn(*a, **kw)
            except Exception:  # noqa: BLE001 - the service must never die
                log.exception("Unhandled error in %s", fn.__name__)
                return default

        return wrapper

    return deco


def setup_logging(debug: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if debug or sys.stderr.isatty():
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)

    def _excepthook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        log.error("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook
    threading.excepthook = lambda args: log.error(  # type: ignore[assignment]
        "Uncaught thread exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def pick_icon(*names: str) -> str:
    """Return the first icon name available in the current icon theme (else the last one)."""
    theme = Gtk.IconTheme.get_default()
    for n in names:
        if theme.has_icon(n):
            return n
    return names[-1]


def format_age(ts: float) -> str:
    """'Just now', '5 min ago', '3 hr ago', 'Yesterday', '4 days ago', 'Mar 02'."""
    delta = int(time.time() - ts)
    if delta < 60:
        return "Just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    then = datetime.fromtimestamp(ts).date()
    days = (date.today() - then).days
    if days <= 0:
        return f"{max(1, delta // 3600)} hr ago"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    return then.strftime("%b %d" if then.year == date.today().year else "%b %d, %Y")


def normalize_text(text: Optional[str], max_bytes: int) -> Optional[tuple[str, str]]:
    """Clean clipboard text. Returns (text, sha256) or None when empty/blank.

    Stores up to `max_bytes` (1 MB default); invalid/non-UTF8 sequences are replaced.
    """
    if not text:
        return None
    text = text.replace("\x00", "")
    raw = text.encode("utf-8", "replace")  # also scrubs lone surrogates
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", "ignore")
    if not text.strip():
        return None
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_preview(text: str) -> str:
    p = text[:PREVIEW_CHARS].strip()
    return re.sub(r"\n\s*\n+", "\n", p.replace("\r", ""))


def pixbuf_fingerprint(pb: "GdkPixbuf.Pixbuf") -> str:
    """Stable hash of the pixel data (independent of PNG encoder output)."""
    w, h, n, rs = pb.get_width(), pb.get_height(), pb.get_n_channels(), pb.get_rowstride()
    data = memoryview(pb.get_pixels())
    digest = hashlib.sha256(f"{w}x{h}x{n}".encode())
    rowlen = w * n
    if rs == rowlen:
        digest.update(data)
    else:  # skip row padding bytes
        for y in range(h):
            digest.update(data[y * rs: y * rs + rowlen])
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    max_items: int = 100            # total history size; oldest unpinned items are dropped first
    auto_paste: bool = False        # press Ctrl+V after selecting (needs xdotool / wtype)
    theme: str = "auto"             # "auto" | "dark" | "light"
    popup_position: str = "cursor"  # "cursor" | "center" | "wm"
    max_text_bytes: int = 1_048_576  # 1 MB stored per text item
    max_image_mb: int = 25          # bigger images are ignored
    use_wl_paste: bool = True       # use `wl-paste --watch` when available
    poll_interval_ms: int = 500     # polling fallback interval
    update_repo: str = ""           # "your-username/clipboard-history" - where updates come from
    check_updates: bool = True      # look for a new version once a day (only if update_repo is set)


def load_config() -> Config:
    cfg = Config()
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for key, default in cfg.__dict__.items():
                if key in data and isinstance(data[key], type(default)) and not (
                        isinstance(default, int) and isinstance(data[key], bool) and not isinstance(default, bool)):
                    setattr(cfg, key, data[key])
        else:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg.__dict__, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        log.exception("Could not read %s, using defaults", CONFIG_PATH)
    cfg.max_items = max(5, min(cfg.max_items, 5000))
    cfg.poll_interval_ms = max(200, min(cfg.poll_interval_ms, 5000))
    cfg.max_text_bytes = max(1024, cfg.max_text_bytes)
    if cfg.theme not in ("auto", "dark", "light"):
        cfg.theme = "auto"
    if cfg.popup_position not in ("cursor", "center", "wm"):
        cfg.popup_position = "cursor"
    return cfg


# --------------------------------------------------------------------------- #
# HistoryStore - SQLite + image files
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    id: int
    kind: str            # "text" | "image"
    image: Optional[str]  # image file name (kind == image)
    thumb: Optional[str]
    hash: str
    preview: str
    pinned: bool
    created: float
    size: int


class HistoryStore:
    """All persistence. Used from the GTK main thread only."""

    def __init__(self, data_dir: Path, max_items: int, max_image_bytes: int) -> None:
        self.max_items = max_items
        self.max_image_bytes = max_image_bytes
        self.images_dir = data_dir / "images"
        self.thumbs_dir = self.images_dir / "thumbs"
        for d in (data_dir, self.images_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(data_dir / "history.db"), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # pins must survive power loss too
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                kind    TEXT NOT NULL CHECK(kind IN ('text','image')),
                content TEXT NOT NULL DEFAULT '',   -- full text, or image file name
                thumb   TEXT,
                hash    TEXT NOT NULL UNIQUE,
                preview TEXT NOT NULL DEFAULT '',
                search  TEXT NOT NULL DEFAULT '',   -- casefolded start of the text
                pinned  INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,              -- time of the most recent copy
                size    INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_items_order ON items(pinned DESC, created DESC);
            """
        )
        self.backup_path = data_dir / "pinned_backup.json"
        self._restore_pins()
        self._export_pins()

    # ---- pinned safety net: text pins are mirrored to a JSON file ------- #
    def _export_pins(self) -> None:
        """Mirror pinned text items to pinned_backup.json (atomic write)."""
        try:
            rows = self.db.execute(
                "SELECT content, hash, created FROM items WHERE pinned=1 AND kind='text' ORDER BY created DESC").fetchall()
            data = [{"content": r["content"], "hash": r["hash"], "created": r["created"]} for r in rows]
            tmp = self.backup_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.backup_path)
        except Exception:  # noqa: BLE001
            log.exception("Could not write pinned backup")

    def _restore_pins(self) -> None:
        """Re-create pinned text items that are missing from the database (e.g. after DB loss)."""
        try:
            if not self.backup_path.exists():
                return
            data = json.loads(self.backup_path.read_text(encoding="utf-8"))
            restored = 0
            for e in data:
                text, digest = e.get("content"), e.get("hash")
                if not isinstance(text, str) or not isinstance(digest, str) or not text.strip():
                    continue
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO items(kind, content, hash, preview, search, pinned, created, size) "
                    "VALUES('text',?,?,?,?,1,?,?)",
                    (text, digest, make_preview(text), text[:20000].casefold(),
                     float(e.get("created") or time.time()), len(text)))
                restored += cur.rowcount
            if restored:
                log.info("Restored %d pinned item(s) from backup", restored)
        except Exception:  # noqa: BLE001
            log.exception("Could not restore pinned backup")

    # ---- queries -------------------------------------------------------- #
    def list_items(self, query: str = "") -> list[Item]:
        sql = ("SELECT id, kind, CASE WHEN kind='image' THEN content ELSE '' END AS image, thumb, hash, "
               "preview, pinned, created, size FROM items")
        params: list[Any] = []
        q = query.strip().casefold()
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " WHERE search LIKE ? ESCAPE '\\'"
            params.append(f"%{esc}%")
        sql += " ORDER BY pinned DESC, created DESC, id DESC"
        return [Item(r["id"], r["kind"], r["image"] or None, r["thumb"], r["hash"], r["preview"],
                     bool(r["pinned"]), r["created"], r["size"]) for r in self.db.execute(sql, params)]

    def get_full(self, item_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT id, kind, content, hash FROM items WHERE id=?", (item_id,)).fetchone()

    def counts(self) -> tuple[int, int]:
        """(total, pinned)"""
        r = self.db.execute("SELECT COUNT(*), COALESCE(SUM(pinned), 0) FROM items").fetchone()
        return int(r[0]), int(r[1])

    def image_path(self, name: str) -> Path:
        return self.images_dir / name

    def _newest_hash(self) -> Optional[str]:
        r = self.db.execute("SELECT hash FROM items ORDER BY created DESC, id DESC LIMIT 1").fetchone()
        return r["hash"] if r else None

    def _bump(self, digest: str) -> bool:
        """Move an existing item to the top. True if it existed."""
        return self.db.execute("UPDATE items SET created=? WHERE hash=?", (time.time(), digest)).rowcount > 0

    # ---- mutations ------------------------------------------------------ #
    def add_text(self, text: str, digest: str) -> bool:
        """Add text. False if it duplicates the newest item; re-copies move to the top."""
        if self._newest_hash() == digest:
            return False
        if self._bump(digest):
            return True
        self.db.execute(
            "INSERT INTO items(kind, content, hash, preview, search, created, size) VALUES('text',?,?,?,?,?,?)",
            (text, digest, make_preview(text), text[:20000].casefold(), time.time(), len(text)))
        self._prune()
        return True

    def add_image(self, pb: "GdkPixbuf.Pixbuf", digest: str) -> bool:
        if self._newest_hash() == digest:
            return False
        if self._bump(digest):
            return True
        name = f"{digest}.png"
        path, thumb_path = self.images_dir / name, self.thumbs_dir / name
        try:
            tmp = self.images_dir / f"{digest}.tmp"
            pb.savev(str(tmp), "png", [], [])
            size = tmp.stat().st_size
            if size > self.max_image_bytes:
                tmp.unlink(missing_ok=True)
                log.info("Image too large (%d bytes), ignored", size)
                return False
            os.replace(tmp, path)
            w, h = pb.get_width(), pb.get_height()
            scale = min(1.0, 540 / w, 240 / h)
            small = pb if scale >= 1 else pb.scale_simple(
                max(1, int(w * scale)), max(1, int(h * scale)), GdkPixbuf.InterpType.BILINEAR)
            small.savev(str(thumb_path), "png", [], [])
            self.db.execute(
                "INSERT INTO items(kind, content, thumb, hash, preview, search, created, size) "
                "VALUES('image',?,?,?,?,?,?,?)",
                (name, name, digest, f"Image {w}\u00d7{h}", "image picture screenshot photo", time.time(), size))
        except Exception:
            path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)
            raise
        self._prune()
        return True

    def touch(self, item_id: int) -> None:
        self.db.execute("UPDATE items SET created=? WHERE id=?", (time.time(), item_id))

    def toggle_pin(self, item_id: int) -> None:
        self.db.execute("UPDATE items SET pinned = 1 - pinned WHERE id=?", (item_id,))
        self._export_pins()

    def delete(self, item_id: int) -> None:
        """Delete one item. Pinned items are protected: unpin first."""
        rows = self.db.execute(
            "SELECT kind, content FROM items WHERE id=? AND pinned=0", (item_id,)).fetchall()
        self.db.execute("DELETE FROM items WHERE id=? AND pinned=0", (item_id,))
        self._remove_files(rows)

    def clear(self, include_pinned: bool = False) -> int:
        """Clear history. Pinned items are ALWAYS kept (include_pinned is ignored on purpose)."""
        where = " WHERE pinned=0"
        rows = self.db.execute(f"SELECT kind, content FROM items{where}").fetchall()
        self.db.execute(f"DELETE FROM items{where}")
        self._remove_files(rows)
        return len(rows)

    def _prune(self) -> None:
        """Enforce max_items by dropping the oldest UNPINNED items. Pinned are never removed."""
        total, _ = self.counts()
        excess = total - self.max_items
        if excess <= 0:
            return
        rows = self.db.execute(
            "SELECT id, kind, content FROM items WHERE pinned=0 ORDER BY created ASC, id ASC LIMIT ?",
            (excess,)).fetchall()
        for r in rows:
            self.db.execute("DELETE FROM items WHERE id=?", (r["id"],))
        self._remove_files(rows)

    def _remove_files(self, rows: list[sqlite3.Row]) -> None:
        for r in rows:
            if r["kind"] == "image" and r["content"]:
                (self.images_dir / r["content"]).unlink(missing_ok=True)
                (self.thumbs_dir / r["content"]).unlink(missing_ok=True)

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------- #
# ClipboardWatcher
# --------------------------------------------------------------------------- #
class WlPasteBackend:
    """Optional backend built on `wl-paste --watch`.

    `wl-paste --watch CMD` runs CMD every time the Wayland clipboard changes. We use
    `echo changed` as CMD, so each change produces one line on our pipe; the actual
    clipboard content is then read with normal `wl-paste` calls (in a worker thread).

    It needs the wlr-data-control protocol (Sway, KDE, Hyprland, ...). GNOME's Mutter
    does NOT implement it, so on GNOME wl-paste exits at once; we detect that, log it and
    simply keep relying on the GTK/XWayland backend, which does work there.
    """

    def __init__(self, on_change: Callable[[], None]) -> None:
        self.on_change = on_change
        self.proc: Optional[subprocess.Popen] = None
        self._watch = 0
        self._started = 0.0
        self._stopped = False

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("wl-paste") and os.environ.get("WAYLAND_DISPLAY"))

    @guarded(False)
    def start(self) -> bool:
        if self._stopped:
            return False
        self.proc = subprocess.Popen(["wl-paste", "--watch", "echo", "changed"], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        os.set_blocking(fd, False)
        self._started = time.monotonic()
        self._watch = GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT,
                                        GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                        self._on_io)
        log.info("wl-paste --watch backend started (pid %s)", self.proc.pid)
        return False  # also usable as a one-shot GLib timeout callback

    @guarded(False)
    def _on_io(self, fd: int, cond: "GLib.IOCondition") -> bool:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return True
        except OSError:
            data = b""
        if data:
            self.on_change()
            return True
        # EOF: wl-paste exited.
        code = self.proc.wait() if self.proc else None
        alive_for = time.monotonic() - self._started
        self._watch = 0
        if alive_for < 3 or self._stopped:
            log.info("wl-paste --watch exited after %.1fs (code %s): unsupported here (normal on GNOME); "
                     "using the GTK clipboard backend only", alive_for, code)
        else:
            log.warning("wl-paste --watch exited (code %s); restarting in 2s", code)
            GLib.timeout_add_seconds(2, self.start)
        return False

    def stop(self) -> None:
        self._stopped = True
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class ClipboardWatcher:
    """Detects clipboard changes and feeds them to the HistoryStore.

    Backends (all feed the same de-duplicating ingest functions):
      1. Gtk "owner-change" signal (primary, works via XWayland on GNOME Wayland and on X11)
      2. 500 ms polling fallback (some owners never emit owner-change)
      3. optional `wl-paste --watch` (see WlPasteBackend)
    """

    def __init__(self, store: HistoryStore, cfg: Config, on_added: Callable[[], None]) -> None:
        self.store, self.cfg, self.on_added = store, cfg, on_added
        self.clipboard: "Gtk.Clipboard" = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self._last_hash: Optional[str] = None
        self._ignore_until = 0.0
        self._last_event = 0.0
        self._pending = False
        self._pending_since = 0.0
        self._debounce = 0
        self._poll_id = 0
        self._wl: Optional[WlPasteBackend] = None
        self._wl_busy = False
        self._wl_dirty = False

    # ---- lifecycle ------------------------------------------------------ #
    def start(self) -> None:
        self.clipboard.connect("owner-change", self._on_owner_change)
        self._poll_id = GLib.timeout_add(self.cfg.poll_interval_ms, self._poll)
        if self.cfg.use_wl_paste and WlPasteBackend.available():
            self._wl = WlPasteBackend(self._wl_trigger)
            self._wl.start()
        else:
            log.info("wl-paste backend not used (disabled, missing or not a Wayland session)")
        self.check()  # capture whatever is on the clipboard right now

    def stop(self) -> None:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        if self._wl:
            self._wl.stop()

    def mark_own(self, digest: Optional[str]) -> None:
        """Call right before WE set the clipboard, so it is not re-recorded as a new copy."""
        self._ignore_until = time.monotonic() + 0.8
        if digest:
            self._last_hash = digest

    # ---- GTK backend ---------------------------------------------------- #
    @guarded()
    def _on_owner_change(self, clipboard: "Gtk.Clipboard", event: Any) -> None:
        self._last_event = time.monotonic()
        if self._debounce:  # apps often clear + set in quick succession
            return
        self._debounce = GLib.timeout_add(60, self._debounced_check)

    @guarded(False)
    def _debounced_check(self) -> bool:
        self._debounce = 0
        self.check()
        return False

    @guarded(True)
    def _poll(self) -> bool:
        if time.monotonic() - self._last_event > 2.0:  # owner-change is quiet: poll
            self.check()
        return True

    @guarded()
    def check(self) -> None:
        now = time.monotonic()
        if self._pending and now - self._pending_since < 3.0:
            return
        self._pending, self._pending_since = True, now
        self.clipboard.request_targets(self._on_targets, None)

    @guarded()
    def _on_targets(self, clipboard: "Gtk.Clipboard", atoms: Any, *rest: Any) -> None:
        try:
            names = {a.name() for a in atoms} if atoms else set()
        except Exception:  # noqa: BLE001
            names = set()
        if not names or "x-kde-passwordManagerHint" in names:  # empty / owner gone / password manager
            self._pending = False
            return
        if names & TEXT_TARGETS:
            clipboard.request_text(self._on_text, None)
        elif any(n.startswith("image/") for n in names):
            clipboard.request_image(self._on_image, None)
        else:
            self._pending = False

    @guarded()
    def _on_text(self, clipboard: "Gtk.Clipboard", text: Optional[str], *rest: Any) -> None:
        self._pending = False
        self._ingest_text(text)

    @guarded()
    def _on_image(self, clipboard: "Gtk.Clipboard", pixbuf: Optional["GdkPixbuf.Pixbuf"], *rest: Any) -> None:
        self._pending = False
        self._ingest_pixbuf(pixbuf)

    # ---- wl-paste backend ------------------------------------------------ #
    @guarded()
    def _wl_trigger(self) -> None:
        if self._wl_busy:
            self._wl_dirty = True
            return
        self._wl_busy = True
        threading.Thread(target=self._wl_worker, daemon=True).start()

    def _wl_worker(self) -> None:
        result: Optional[tuple[str, bytes]] = None
        try:
            result = self._wl_read()
        except Exception:  # noqa: BLE001
            log.exception("wl-paste read failed")
        GLib.idle_add(self._wl_done, result)

    def _wl_read(self) -> Optional[tuple[str, bytes]]:
        types_proc = subprocess.run(["wl-paste", "--list-types"], capture_output=True, timeout=2)
        if types_proc.returncode != 0:
            return None  # empty clipboard
        types = types_proc.stdout.decode("utf-8", "replace").split()
        if "x-kde-passwordManagerHint" in types:
            return None
        if any(t in TEXT_TARGETS or t.startswith("text/plain") for t in types):
            out = subprocess.run(["wl-paste", "--no-newline", "--type", "text"], capture_output=True, timeout=3)
            return ("text", out.stdout) if out.returncode == 0 else None
        if "image/png" in types:
            out = subprocess.run(["wl-paste", "--type", "image/png"], capture_output=True, timeout=5)
            return ("image", out.stdout) if out.returncode == 0 else None
        return None

    @guarded(False)
    def _wl_done(self, result: Optional[tuple[str, bytes]]) -> bool:
        self._wl_busy = False
        try:
            if result:
                kind, data = result
                if kind == "text":
                    self._ingest_text(data.decode("utf-8", "replace"))
                else:
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(data)
                    loader.close()
                    self._ingest_pixbuf(loader.get_pixbuf())
        finally:
            if self._wl_dirty:
                self._wl_dirty = False
                self._wl_trigger()
        return False

    # ---- shared ingest --------------------------------------------------- #
    def _suppressed(self, digest: str) -> bool:
        if time.monotonic() < self._ignore_until:  # this change was caused by us
            self._last_hash = digest
            return True
        return False

    def _ingest_text(self, text: Optional[str]) -> None:
        norm = normalize_text(text, self.cfg.max_text_bytes)
        if norm is None:
            return
        text, digest = norm
        if self._suppressed(digest) or digest == self._last_hash:
            return
        self._last_hash = digest
        if self.store.add_text(text, digest):
            log.debug("stored text (%d chars)", len(text))
            self.on_added()

    def _ingest_pixbuf(self, pb: Optional["GdkPixbuf.Pixbuf"]) -> None:
        if pb is None or pb.get_width() < 1 or pb.get_height() < 1:
            return
        if pb.get_width() * pb.get_height() > 100_000_000:
            return
        digest = pixbuf_fingerprint(pb)
        if self._suppressed(digest) or digest == self._last_hash:
            return
        self._last_hash = digest
        if self.store.add_image(pb, digest):
            log.debug("stored image %dx%d", pb.get_width(), pb.get_height())
            self.on_added()


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
DARK_PALETTE = {
    "window_bg": "rgba(32,32,32,0.92)",
    "window_solid": "#202020",
    "border_color": "rgba(255,255,255,0.08)",
    "shadow_color": "rgba(0,0,0,0.45)",
    "text": "#FFFFFF",
    "text_secondary": "#9D9D9D",
    "accent": "#60CDFF",
    "card_bg": "rgba(255,255,255,0.05)",
    "card_hover": "rgba(255,255,255,0.09)",
    "search_bg": "rgba(255,255,255,0.06)",
    "search_bg_focus": "rgba(255,255,255,0.09)",
    "search_border": "rgba(255,255,255,0.20)",
    "popover_bg": "#2C2C2C",
    "danger": "#FF99A4",
    "scroll_thumb": "rgba(255,255,255,0.38)",
}
LIGHT_PALETTE = {
    "window_bg": "rgba(243,243,243,0.94)",
    "window_solid": "#F3F3F3",
    "border_color": "rgba(0,0,0,0.10)",
    "shadow_color": "rgba(0,0,0,0.25)",
    "text": "#1A1A1A",
    "text_secondary": "#616161",
    "accent": "#005FB8",
    "card_bg": "rgba(255,255,255,0.80)",
    "card_hover": "rgba(0,0,0,0.06)",
    "search_bg": "rgba(0,0,0,0.05)",
    "search_bg_focus": "rgba(255,255,255,0.90)",
    "search_border": "rgba(0,0,0,0.30)",
    "popover_bg": "#FBFBFB",
    "danger": "#C42B1C",
    "scroll_thumb": "rgba(0,0,0,0.38)",
}


class ThemeManager:
    """Loads style.css with the right @define-color palette; follows the system preference."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.dark = True
        self._provider: Optional["Gtk.CssProvider"] = None
        self._applied: Optional[bool] = None

    def _system_prefers_dark(self) -> bool:
        try:
            src = Gio.SettingsSchemaSource.get_default()
            schema = src.lookup("org.gnome.desktop.interface", True) if src else None
            if schema:
                s = Gio.Settings.new("org.gnome.desktop.interface")
                scheme = s.get_string("color-scheme") if schema.has_key("color-scheme") else "default"
                theme = s.get_string("gtk-theme").lower() if schema.has_key("gtk-theme") else ""
                if scheme == "prefer-dark":
                    return True
                if scheme == "prefer-light":
                    return False
                if "dark" in theme:
                    return True
                if theme:
                    return False
        except Exception:  # noqa: BLE001
            log.debug("gsettings theme detection failed", exc_info=True)
        st = Gtk.Settings.get_default()
        if st is not None:
            if st.get_property("gtk-application-prefer-dark-theme"):
                return True
            name = (st.get_property("gtk-theme-name") or "").lower()
            if name:
                return "dark" in name
        return True  # dark by default

    def apply(self) -> None:
        """(Re)load the CSS if the light/dark choice changed since last time."""
        dark = {"dark": True, "light": False}.get(self.cfg.theme, None)
        if dark is None:
            dark = self._system_prefers_dark()
        if dark == self._applied:
            return
        palette = DARK_PALETTE if dark else LIGHT_PALETTE
        defines = "".join(f"@define-color {k} {v};\n" for k, v in palette.items())
        try:
            css = (APP_DIR / "style.css").read_text(encoding="utf-8")
        except OSError:
            log.error("style.css not found next to %s", __file__)
            css = ""
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data((defines + css).encode("utf-8"))
        except GLib.Error:
            log.exception("CSS failed to parse")
            return
        screen = Gdk.Screen.get_default()
        if self._provider is not None:
            Gtk.StyleContext.remove_provider_for_screen(screen, self._provider)
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._provider, self._applied, self.dark = provider, dark, dark
        log.info("Applied %s theme", "dark" if dark else "light")


# --------------------------------------------------------------------------- #
# UI widgets
# --------------------------------------------------------------------------- #
class RoundedImage(Gtk.DrawingArea):
    """Draws a pixbuf with rounded corners (GTK 3 CSS cannot clip images)."""

    def __init__(self, pixbuf: "GdkPixbuf.Pixbuf", radius: float = 4.0) -> None:
        super().__init__()
        self._pb, self._r = pixbuf, radius
        self.set_size_request(pixbuf.get_width(), pixbuf.get_height())
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.START)
        self.connect("draw", self._on_draw)

    @staticmethod
    def _arc_path(cr: Any, w: float, h: float, r: float) -> None:
        import math
        cr.new_sub_path()
        cr.arc(w - r, r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _on_draw(self, _w: Any, cr: Any) -> bool:
        self._arc_path(cr, self._pb.get_width(), self._pb.get_height(), self._r)
        cr.clip()
        Gdk.cairo_set_source_pixbuf(cr, self._pb, 0, 0)
        cr.paint()
        return False


class PopupWindow(Gtk.Window):
    """The Win+V style panel."""

    def __init__(self, app: "Application") -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app, self.cfg, self.store = app, app.cfg, app.store
        self.active_tab = "clipboard"
        self._items: list[Item] = []
        self._cards: list[Gtk.Widget] = []
        self._menu_buttons: list[Gtk.Button] = []
        self._sel = -1
        self._armed = False               # close-on-focus-loss only after we were active once
        self._open_popover: Optional["Gtk.Popover"] = None
        self._dragging = False            # a window-move drag is in progress
        self._drag_end_id = 0
        self._user_moved = False          # user dragged the window during this showing
        self._anim_id = 0
        self._thumb_cache: dict[str, Optional["GdkPixbuf.Pixbuf"]] = {}
        self._emoji_built = False
        self._emoji_query = ""
        self._emoji_lookup: dict[str, str] = {}
        self._emoji_groups: list[tuple[Gtk.Label, Gtk.FlowBox, list[str]]] = []

        # Window setup: undecorated, RGBA visual when a compositor exists.
        self.set_title("Clipboard history")
        self.set_role(APP_NAME)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_icon_name("edit-paste")
        self.get_style_context().add_class("ch-window")
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        self.use_alpha = bool(visual is not None and screen.is_composited())
        if self.use_alpha:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.shadow_m = SHADOW_MARGIN if self.use_alpha else 0

        self._build_ui()
        self.connect("key-press-event", self._on_key)
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("delete-event", lambda *_: self.hide_on_delete())

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        m = self.shadow_m
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.get_style_context().add_class("ch-frame")
        if not self.use_alpha:
            frame.get_style_context().add_class("solid")
        frame.set_size_request(FRAME_W, FRAME_H)
        for side in ("top", "bottom", "start", "end"):
            getattr(frame, f"set_margin_{side}")(m)
        self.add(frame)

        # Drag grip (top strip): press and drag to move the window
        grip = Gtk.EventBox()
        grip.set_visible_window(False)
        grip.set_size_request(-1, 14)
        grip_bar = Gtk.Box()
        grip_bar.set_size_request(36, 4)
        grip_bar.set_halign(Gtk.Align.CENTER)
        grip_bar.set_valign(Gtk.Align.CENTER)
        grip_bar.get_style_context().add_class("grip-bar")
        grip.add(grip_bar)
        self._make_drag_handle(grip)
        frame.pack_start(grip, False, False, 0)

        # Tab bar
        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        tabs.set_margin_top(0)
        tabs.set_margin_start(12)
        tabs.set_margin_end(12)
        self._tab_btns: dict[str, Gtk.Button] = {}
        self._tab_pills: dict[str, Gtk.Box] = {}
        for name, icon, tip in (("clipboard", pick_icon("edit-paste-symbolic", "edit-paste"), "Clipboard history"),
                                ("emoji", pick_icon("face-smile-symbolic", "face-smile"), "Emoji")):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.set_can_focus(False)
            btn.set_focus_on_click(False)
            btn.set_tooltip_text(tip)
            img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            img.set_pixel_size(18)
            btn.add(img)
            btn.get_style_context().add_class("tab-btn")
            btn.connect("clicked", lambda _b, n=name: self.switch_tab(n))
            pill = Gtk.Box()
            pill.set_size_request(16, 3)
            pill.set_halign(Gtk.Align.CENTER)
            pill.get_style_context().add_class("tab-pill")
            col.pack_start(btn, False, False, 0)
            col.pack_start(pill, False, False, 0)
            tabs.pack_start(col, True, True, 0)
            self._tab_btns[name], self._tab_pills[name] = btn, pill
        frame.pack_start(tabs, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(100)
        self.stack.add_named(self._build_clipboard_page(), "clipboard")
        frame.pack_start(self.stack, True, True, 0)
        self._update_tab_styles()

    def set_update_available(self, version: str) -> None:
        self.update_btn.set_label("Update")   # short on purpose: a long label would widen the popup
        self.update_btn.set_tooltip_text(f"Install version {version} and restart")
        self.update_btn.show()

    @guarded(None)
    def _on_update_clicked(self, *_: Any) -> None:
        self.hide_popup()
        subprocess.Popen([sys.executable, str(APP_DIR / "updater.py"), "update"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _make_drag_handle(self, widget: Gtk.EventBox) -> None:
        widget.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        widget.connect("realize", lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "grab")))
        widget.connect("button-press-event", self._on_drag_press)

    @guarded(False)
    def _on_drag_press(self, _w: Gtk.Widget, event: "Gdk.EventButton") -> bool:
        if event.button != 1:
            return False
        self._dragging = True
        self._user_moved = True
        self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        GLib.timeout_add(600, self._end_drag_soon)
        return True

    def _end_drag_soon(self) -> bool:
        # begin_move_drag swallows the button release, so end the "dragging" state on a timer
        # that re-arms while the window is still being moved.
        pos = self.get_position()
        if getattr(self, "_last_drag_pos", None) != pos:
            self._last_drag_pos = pos
            return True
        self._dragging = False
        self._last_drag_pos = None
        return False

    def _make_search(self, placeholder: str) -> Gtk.Entry:
        e = Gtk.Entry()
        e.set_placeholder_text(placeholder)
        e.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, pick_icon("system-search-symbolic", "edit-find-symbolic"))
        e.get_style_context().add_class("search")
        return e

    def _build_clipboard_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_start(12)
        page.set_margin_end(12)
        page.set_margin_top(6)
        page.set_margin_bottom(12)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title = Gtk.Label(label="Clipboard history", xalign=0)
        title.get_style_context().add_class("title")
        title_handle = Gtk.EventBox()
        title_handle.set_visible_window(False)
        title_handle.add(title)
        title_handle.set_tooltip_text("Drag to move")
        self._make_drag_handle(title_handle)
        header.pack_start(title_handle, True, True, 0)
        self.clear_btn = Gtk.Button(label="Clear all")
        self.clear_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.clear_btn.set_can_focus(False)
        self.clear_btn.set_focus_on_click(False)
        self.clear_btn.get_style_context().add_class("link-btn")
        self.clear_btn.connect("clicked", self._on_clear_clicked)
        header.pack_end(self.clear_btn, False, False, 0)
        self.update_btn = Gtk.Button(label="Update")
        self.update_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.update_btn.set_can_focus(False)
        self.update_btn.set_focus_on_click(False)
        self.update_btn.get_style_context().add_class("link-btn")
        self.update_btn.get_style_context().add_class("update-btn")
        self.update_btn.set_no_show_all(True)   # only shown when a newer version exists
        self.update_btn.connect("clicked", self._on_update_clicked)
        header.pack_end(self.update_btn, False, False, 0)
        page.pack_start(header, False, False, 0)

        self.search = self._make_search("Search clipboard history")
        self.search.connect("changed", lambda *_: self._rebuild())
        page.pack_start(self.search, False, False, 0)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.list_box.get_style_context().add_class("ch-list")
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_shadow_type(Gtk.ShadowType.NONE)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_overlay_scrolling(True)
        self.scroller.set_kinetic_scrolling(True)
        self.scroller.add(self.list_box)

        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        empty.set_valign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name(pick_icon("edit-paste-symbolic", "edit-paste"), Gtk.IconSize.DIALOG)
        icon.set_pixel_size(48)
        icon.get_style_context().add_class("empty-icon")
        self.empty_label = Gtk.Label(label="")
        self.empty_label.set_line_wrap(True)
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.set_max_width_chars(30)
        self.empty_label.get_style_context().add_class("empty-text")
        empty.pack_start(icon, False, False, 0)
        empty.pack_start(self.empty_label, False, False, 0)

        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(self.scroller, "list")
        self.list_stack.add_named(empty, "empty")
        page.pack_start(self.list_stack, True, True, 0)

        # Confirmation popover for "Clear all"
        self.clear_pop = Gtk.Popover()
        self.clear_pop.set_relative_to(self.clear_btn)
        self.clear_pop.set_position(Gtk.PositionType.BOTTOM)
        self.clear_pop.get_style_context().add_class("ch-popover")
        self.clear_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.clear_box.set_border_width(6)
        self.clear_pop.add(self.clear_box)
        self._track_popover(self.clear_pop)
        return page

    # ---------------------------------------------------------- emoji tab --
    def _build_emoji_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_start(12)
        page.set_margin_end(12)
        page.set_margin_top(6)
        page.set_margin_bottom(12)
        self.emoji_search = self._make_search("Search emojis")
        self.emoji_search.connect("changed", self._on_emoji_search)
        page.pack_start(self.emoji_search, False, False, 0)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.get_style_context().add_class("ch-list")
        for cat, entries in EMOJI_CATEGORIES:
            label = Gtk.Label(label=cat, xalign=0)
            label.get_style_context().add_class("section-label")
            flow = Gtk.FlowBox()
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_homogeneous(True)
            flow.set_min_children_per_line(8)
            flow.set_max_children_per_line(8)
            flow.set_row_spacing(2)
            flow.set_column_spacing(2)
            flow.set_valign(Gtk.Align.START)
            flow.set_filter_func(self._emoji_filter, None)
            names: list[str] = []
            for entry in entries:
                emoji, words = parse_entry(entry)
                self._emoji_lookup[emoji] = words
                names.append(words)
                btn = Gtk.Button(label=emoji)
                btn.set_relief(Gtk.ReliefStyle.NONE)
                btn.set_can_focus(False)
                btn.set_focus_on_click(False)
                btn.set_tooltip_text(" ".join(words.split()[:3]))
                btn.get_style_context().add_class("emoji-btn")
                btn.connect("clicked", lambda _b, e=emoji: self._on_emoji_clicked(e))
                flow.add(btn)
            inner.pack_start(label, False, False, 0)
            inner.pack_start(flow, False, False, 0)
            self._emoji_groups.append((label, flow, names))
        self.emoji_none = Gtk.Label(label="No emojis found")
        self.emoji_none.get_style_context().add_class("empty-text")
        self.emoji_none.set_no_show_all(True)
        inner.pack_start(self.emoji_none, False, False, 24)

        sc = Gtk.ScrolledWindow()
        sc.set_shadow_type(Gtk.ShadowType.NONE)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_overlay_scrolling(True)
        sc.add(inner)
        page.pack_start(sc, True, True, 0)
        self._emoji_built = True
        return page

    def _emoji_filter(self, child: "Gtk.FlowBoxChild", _data: Any) -> bool:
        if not self._emoji_query:
            return True
        btn = child.get_child()
        return self._emoji_query in self._emoji_lookup.get(btn.get_label(), "")

    @guarded()
    def _on_emoji_search(self, entry: Gtk.Entry) -> None:
        self._emoji_query = entry.get_text().strip().casefold()
        any_visible = False
        for label, flow, names in self._emoji_groups:
            has = (not self._emoji_query) or any(self._emoji_query in n for n in names)
            flow.invalidate_filter()
            label.set_visible(has)
            flow.set_visible(has)
            any_visible = any_visible or has
        self.emoji_none.set_visible(not any_visible)

    def _emoji_first_match(self) -> Optional[str]:
        for _cat, entries in EMOJI_CATEGORIES:
            for entry in entries:
                emoji, words = parse_entry(entry)
                if self._emoji_query in words:
                    return emoji
        return None

    @guarded()
    def _on_emoji_clicked(self, emoji: str) -> None:
        self.app.copy_text(emoji)
        self._finish_copy()

    # --------------------------------------------------------------- tabs --
    def _update_tab_styles(self) -> None:
        for name in self._tab_btns:
            for w in (self._tab_btns[name], self._tab_pills[name]):
                ctx = w.get_style_context()
                (ctx.add_class if name == self.active_tab else ctx.remove_class)("active")

    def switch_tab(self, name: str, focus: bool = True) -> None:
        if name == "emoji" and not self._emoji_built:
            page = self._build_emoji_page()
            self.stack.add_named(page, "emoji")
            page.show_all()
        self.active_tab = name
        self.stack.set_visible_child_name(name)
        self._update_tab_styles()
        if focus:
            self._focus_entry()

    def _focus_entry(self) -> None:
        e = self.search if self.active_tab == "clipboard" else getattr(self, "emoji_search", self.search)
        e.grab_focus_without_selecting()

    # ---------------------------------------------------------- list build --
    def _thumb(self, item: Item) -> Optional["GdkPixbuf.Pixbuf"]:
        key = item.thumb or item.image or ""
        if key in self._thumb_cache:
            return self._thumb_cache[key]
        if len(self._thumb_cache) > 150:
            self._thumb_cache.clear()
        pb: Optional["GdkPixbuf.Pixbuf"] = None
        try:
            raw = GdkPixbuf.Pixbuf.new_from_file(str(self.store.thumbs_dir / key))
            w, h = raw.get_width(), raw.get_height()
            s = min(1.0, THUMB_MAX_W / w, THUMB_MAX_H / h)
            pb = raw if s >= 1 else raw.scale_simple(max(1, int(w * s)), max(1, int(h * s)),
                                                     GdkPixbuf.InterpType.BILINEAR)
        except (GLib.Error, ZeroDivisionError):
            pb = None
        self._thumb_cache[key] = pb
        return pb

    def _section_label(self, text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.get_style_context().add_class("section-label")
        return lbl

    @guarded()
    def _rebuild(self, select_id: Optional[int] = None, keep_index: Optional[int] = None) -> None:
        query = self.search.get_text().strip()
        items = self.store.list_items(query)
        self._open_popover = None
        for child in self.list_box.get_children():
            child.destroy()
        self._items, self._cards, self._menu_buttons, self._sel = items, [], [], -1

        if not items:
            self.empty_label.set_text(f"No matches for \u201c{query}\u201d" if query
                                      else "Nothing here yet. Copy something and it will appear.")
            self.list_stack.set_visible_child_name("empty")
        else:
            self.list_stack.set_visible_child_name("list")
            last: Optional[bool] = None
            for it in items:
                if it.pinned != last:
                    last = it.pinned
                    self.list_box.pack_start(self._section_label("Pinned" if it.pinned else "Recent"), False, False, 0)
                card, more = self._make_card(it)
                self.list_box.pack_start(card, False, False, 0)
                self._cards.append(card)
                self._menu_buttons.append(more)
            self.list_box.show_all()
            idx, scroll = 0, False
            if select_id is not None:
                idx = next((i for i, it in enumerate(items) if it.id == select_id), 0)
                scroll = True
            elif keep_index is not None:
                idx, scroll = min(keep_index, len(items) - 1), True
            else:
                self.scroller.get_vadjustment().set_value(0)
            self._select(idx, scroll)
        self.clear_btn.set_sensitive(self.store.counts()[0] > 0)

    def _make_card(self, item: Item) -> tuple[Gtk.EventBox, Gtk.Button]:
        card = Gtk.EventBox()
        card.set_visible_window(True)
        card.get_style_context().add_class("card")
        card.add_events(Gdk.EventMask.BUTTON_RELEASE_MASK)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # GtkEventBox ignores CSS padding/margin on GTK 3.24, so spacing is set on the widgets.
        row.set_margin_top(10)
        row.set_margin_bottom(10)
        row.set_margin_start(10)
        row.set_margin_end(8)
        card.set_margin_bottom(6)
        card.add(row)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.pack_start(left, True, True, 0)
        if item.kind == "image":
            pb = self._thumb(item)
            if pb is not None:
                left.pack_start(RoundedImage(pb), False, False, 0)
            else:
                lbl = Gtk.Label(label=item.preview + " (file missing)", xalign=0)
                lbl.get_style_context().add_class("card-text")
                left.pack_start(lbl, False, False, 0)
        else:
            lbl = Gtk.Label(label=item.preview, xalign=0, yalign=0)
            lbl.set_line_wrap(True)
            lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lbl.set_lines(3)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_width_chars(10)
            lbl.set_max_width_chars(10)
            lbl.set_hexpand(True)
            lbl.get_style_context().add_class("card-text")
            left.pack_start(lbl, False, False, 0)
        age = Gtk.Label(label=format_age(item.created), xalign=0)
        age.get_style_context().add_class("card-time")
        left.pack_start(age, False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        right.set_valign(Gtk.Align.START)
        right.set_halign(Gtk.Align.END)
        pin_toggle = Gtk.Button()
        pin_toggle.set_relief(Gtk.ReliefStyle.NONE)
        pin_toggle.set_can_focus(False)
        pin_toggle.set_focus_on_click(False)
        pin_toggle.set_tooltip_text("Unpin" if item.pinned else "Pin")
        pin_toggle.add(Gtk.Image.new_from_icon_name(
            pick_icon("view-pin-symbolic", "starred-symbolic", "emblem-favorite-symbolic"), Gtk.IconSize.MENU))
        pin_toggle.get_style_context().add_class("pin-btn")
        if item.pinned:
            pin_toggle.get_style_context().add_class("pinned")
        pin_toggle.connect("clicked", lambda *_: GLib.idle_add(self._toggle_pin, item.id))
        right.pack_start(pin_toggle, False, False, 0)
        more = Gtk.Button()
        more.set_relief(Gtk.ReliefStyle.NONE)
        more.set_can_focus(False)
        more.set_focus_on_click(False)
        more.set_tooltip_text("More options")
        more.add(Gtk.Image.new_from_icon_name(pick_icon("view-more-symbolic", "open-menu-symbolic"), Gtk.IconSize.MENU))
        more.get_style_context().add_class("more-btn")
        right.pack_start(more, False, False, 0)
        row.pack_start(right, False, False, 0)

        # "..." popover with Pin/Unpin + Delete
        pop = Gtk.Popover()
        pop.set_relative_to(more)
        pop.set_position(Gtk.PositionType.BOTTOM)
        pop.get_style_context().add_class("ch-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(4)
        pin_btn = self._menu_item("Unpin" if item.pinned else "Pin")
        pin_btn.connect("clicked", lambda *_: (pop.popdown(), GLib.idle_add(self._toggle_pin, item.id)))
        box.pack_start(pin_btn, False, False, 0)
        if not item.pinned:  # pinned items can't be deleted until unpinned
            del_btn = self._menu_item("Delete", danger=True)
            del_btn.connect("clicked", lambda *_: (pop.popdown(), GLib.idle_add(self._delete, item.id, None)))
            box.pack_start(del_btn, False, False, 0)
        box.show_all()
        pop.add(box)
        self._track_popover(pop, card)
        more.connect("clicked", lambda *_: pop.popup())
        card.connect("destroy", lambda *_: pop.destroy())
        card.connect("button-release-event", self._on_card_release, item, more)
        return card, more

    @staticmethod
    def _menu_item(text: str, danger: bool = False) -> Gtk.Button:
        b = Gtk.Button(label=text)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_can_focus(False)
        b.set_focus_on_click(False)
        child = b.get_child()
        if isinstance(child, Gtk.Label):
            child.set_xalign(0)
        ctx = b.get_style_context()
        ctx.add_class("menu-item")
        if danger:
            ctx.add_class("danger")
        return b

    def _track_popover(self, pop: "Gtk.Popover", card: Optional[Gtk.Widget] = None) -> None:
        def shown(*_: Any) -> None:
            self._open_popover = pop
            if card is not None:
                card.get_style_context().add_class("menu-open")

        def closed(*_: Any) -> None:
            if self._open_popover is pop:
                self._open_popover = None
            if card is not None:
                card.get_style_context().remove_class("menu-open")
            if self.get_visible():
                self._focus_entry()

        pop.connect("show", shown)
        pop.connect("closed", closed)

    @guarded()
    def _on_card_release(self, _card: Gtk.Widget, event: "Gdk.EventButton", item: Item, more: Gtk.Button) -> bool:
        if self._open_popover is not None:
            return False
        if event.button == 1:
            self._activate(item)
            return True
        if event.button == 3:  # right click opens the same menu
            more.clicked()
            return True
        return False

    # ------------------------------------------------------------ selection --
    def _select(self, idx: int, scroll: bool = True) -> None:
        if not self._cards:
            self._sel = -1
            return
        idx = max(0, min(idx, len(self._cards) - 1))
        if 0 <= self._sel < len(self._cards):
            self._cards[self._sel].get_style_context().remove_class("selected")
        self._sel = idx
        self._cards[idx].get_style_context().add_class("selected")
        if scroll:
            GLib.idle_add(self._scroll_to, idx)

    def _scroll_to(self, idx: int) -> bool:
        if 0 <= idx < len(self._cards):
            alloc = self._cards[idx].get_allocation()
            top = 0 if idx == 0 else alloc.y
            self.scroller.get_vadjustment().clamp_page(top, alloc.y + alloc.height + 6)
        return False

    def _selected_item(self) -> Optional[Item]:
        return self._items[self._sel] if 0 <= self._sel < len(self._items) else None

    # -------------------------------------------------------------- actions --
    @guarded()
    def _activate(self, item: Item) -> None:
        ok = self.app.copy_item(item.id)
        self._finish_copy(ok)

    def _finish_copy(self, ok: bool = True) -> None:
        self.hide_popup()
        if ok and self.cfg.auto_paste:
            self.app.schedule_auto_paste()

    @guarded(False)
    def _toggle_pin(self, item_id: int) -> bool:
        self.store.toggle_pin(item_id)
        self._rebuild(select_id=item_id)
        self._focus_entry()
        return False

    @guarded(False)
    def _delete(self, item_id: int, keep_index: Optional[int]) -> bool:
        idx = self._sel if keep_index is None else keep_index
        self.store.delete(item_id)
        self._rebuild(keep_index=max(idx, 0))
        self._focus_entry()
        return False

    @guarded()
    def _on_clear_clicked(self, _btn: Gtk.Button) -> None:
        total, pinned = self.store.counts()
        if total == 0:
            return
        unpinned = total - pinned
        for c in self.clear_box.get_children():
            c.destroy()
        title = Gtk.Label(label="Clear clipboard history?", xalign=0)
        title.get_style_context().add_class("popover-title")
        self.clear_box.pack_start(title, False, False, 0)
        if unpinned > 0:
            label = f"Clear all ({pinned} pinned kept)" if pinned else "Clear all"
            b = self._menu_item(label)
            b.connect("clicked", lambda *_: self._do_clear(False))
            self.clear_box.pack_start(b, False, False, 0)
        if pinned > 0 and unpinned == 0:
            note = Gtk.Label(label=f"Only {pinned} pinned item(s) left. Unpin an item to delete it.", xalign=0)
            note.set_line_wrap(True)
            note.set_max_width_chars(28)
            note.get_style_context().add_class("popover-title")
            self.clear_box.pack_start(note, False, False, 0)
        self.clear_box.show_all()
        self.clear_pop.popup()

    def _do_clear(self, include_pinned: bool) -> None:
        self.clear_pop.popdown()
        n = self.store.clear(include_pinned)
        log.info("Cleared %d items (include_pinned=%s)", n, include_pinned)
        self._rebuild()

    # ------------------------------------------------------------- keyboard --
    @guarded(False)
    def _on_key(self, _w: Gtk.Widget, event: "Gdk.EventKey") -> bool:
        key = event.keyval
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if key == Gdk.KEY_Escape:
            if self._open_popover is not None:
                self._open_popover.popdown()
            else:
                self.hide_popup()
            return True
        if self._open_popover is not None:
            return False
        if ctrl and key in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self.switch_tab("emoji" if self.active_tab == "clipboard" else "clipboard")
            return True
        if self.active_tab == "emoji":
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                first = self._emoji_first_match()
                if first:
                    self._on_emoji_clicked(first)
                return True
            return False

        item = self._selected_item()
        if ctrl and key in (Gdk.KEY_p, Gdk.KEY_P):
            if item:
                self.store.toggle_pin(item.id)
                self._rebuild(select_id=item.id)
            return True
        if key == Gdk.KEY_Down:
            self._select(self._sel + 1)
            return True
        if key == Gdk.KEY_Up:
            self._select(self._sel - 1)
            return True
        if key == Gdk.KEY_Page_Down:
            self._select(self._sel + 5)
            return True
        if key == Gdk.KEY_Page_Up:
            self._select(self._sel - 5)
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if item:
                self._activate(item)
            return True
        if key == Gdk.KEY_Delete:
            text = self.search.get_text()
            if text and self.search.get_position() < len(text):
                return False  # let the entry delete a character
            if item and not item.pinned:  # pinned items are protected: unpin first
                self._delete(item.id, self._sel)
            return True
        return False

    # ----------------------------------------------------- show / hide / pos --
    def _on_active_changed(self, *_: Any) -> None:
        if self.is_active():
            self._armed = True
        elif self._armed and self.get_visible() and not self._dragging:
            GLib.timeout_add(100, self._close_if_inactive)  # small debounce for focus flapping

    @guarded(False)
    def _close_if_inactive(self) -> bool:
        if self.get_visible() and not self.is_active() and not self._dragging:
            self.hide_popup()
        return False

    def _compute_position(self) -> Optional[tuple[int, int]]:
        """Top-left of the WINDOW so that the panel sits near the cursor, fully on screen."""
        if self.cfg.popup_position == "wm":
            return None  # let the window manager pick the monitor under the mouse
        display = Gdk.Display.get_default()
        m, fw, fh = self.shadow_m, FRAME_W, FRAME_H
        px = py = None
        if self.cfg.popup_position == "cursor":
            try:
                _screen, x, y = display.get_default_seat().get_pointer().get_position()
                if not (x == 0 and y == 0):  # (0,0) = "unknown" under XWayland
                    px, py = x, y
            except Exception:  # noqa: BLE001
                log.debug("pointer position unavailable", exc_info=True)
        if px is None:
            mon = display.get_primary_monitor() or display.get_monitor(0)
            wa = mon.get_workarea()
            fx, fy = wa.x + (wa.width - fw) // 2, wa.y + (wa.height - fh) // 2
        else:
            mon = display.get_monitor_at_point(px, py) or display.get_primary_monitor() or display.get_monitor(0)
            wa = mon.get_workarea()
            fx, fy = px + 8, py + 8
            if fx + fw > wa.x + wa.width:      # no room to the right: open to the left of the cursor
                fx = px - fw - 8
            if fy + fh > wa.y + wa.height:     # no room below: open above the cursor
                fy = py - fh - 8
        fx = max(wa.x + 4, min(fx, wa.x + wa.width - fw - 4))
        fy = max(wa.y + 4, min(fy, wa.y + wa.height - fh - 4))
        return fx - m, fy - m

    @staticmethod
    def _monitor_key(mon: "Gdk.Monitor") -> str:
        g = mon.get_geometry()
        return f"{g.x},{g.y},{g.width}x{g.height}"

    def _load_positions(self) -> dict:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8")).get("positions", {})
        except (OSError, ValueError, AttributeError):
            return {}

    def _monitor_of_window(self) -> tuple[Optional["Gdk.Monitor"], int, int]:
        x, y = self.get_position()
        fx, fy = x + self.shadow_m, y + self.shadow_m
        display = Gdk.Display.get_default()
        mon = display.get_monitor_at_point(fx + FRAME_W // 2, fy + FRAME_H // 2)
        return mon, fx, fy

    @guarded(False)
    def _place_from_memory(self, target: Optional[tuple[int, int]] = None, attempt: int = 0) -> bool:
        """Move the freshly shown window to the spot the user last dragged it to on this monitor.

        Window managers sometimes apply their own placement just after mapping, so the move is
        verified and retried a few times before the window is faded in.
        """
        if not self.get_visible():
            return False
        if attempt == 0:
            mon, _fx, _fy = self._monitor_of_window()
            saved = self._load_positions().get(self._monitor_key(mon)) if mon else None
            if saved and len(saved) == 2:
                g = mon.get_workarea()
                fx = max(g.x + 4, min(g.x + int(saved[0]), g.x + g.width - FRAME_W - 4))
                fy = max(g.y + 4, min(g.y + int(saved[1]), g.y + g.height - FRAME_H - 4))
                target = (fx - self.shadow_m, fy - self.shadow_m)
        if target is not None and not self._user_moved:
            cx, cy = self.get_position()
            if abs(cx - target[0]) > 2 or abs(cy - target[1]) > 2:
                self.move(*target)
                if attempt < 5:
                    GLib.timeout_add(80, self._place_from_memory, target, attempt + 1)
                    return False
        self._fade_in()
        return False

    def _fade_in(self) -> None:
        if not self.use_alpha:
            return
        start = time.monotonic()

        def tick() -> bool:
            t = min(1.0, (time.monotonic() - start) / 0.12)
            self.set_opacity(1 - (1 - t) ** 3)
            if t >= 1.0:
                self._anim_id = 0
                return False
            return True

        if self._anim_id:
            GLib.source_remove(self._anim_id)
        self._anim_id = GLib.timeout_add(12, tick)

    def _save_position(self) -> None:
        """Remember where the user dragged the window, relative to its monitor's work area."""
        try:
            mon, fx, fy = self._monitor_of_window()
            if mon is None:
                return
            g = mon.get_workarea()
            positions = self._load_positions()
            positions[self._monitor_key(mon)] = [fx - g.x, fy - g.y]
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"positions": positions}), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
        except Exception:  # noqa: BLE001
            log.exception("could not save popup position")

    def _server_time(self) -> int:
        gw = self.get_window()
        if GdkX11 is not None and gw is not None and isinstance(gw, GdkX11.X11Window):
            return GdkX11.x11_get_server_time(gw)
        return Gdk.CURRENT_TIME

    @guarded()
    def show_popup(self) -> None:
        self.app.theme.apply()
        self.search.set_text("")
        if self._emoji_built:
            self.emoji_search.set_text("")
        self.switch_tab("clipboard", focus=False)
        self._rebuild()
        pos = self._compute_position()
        self._armed = False
        self._user_moved = False
        self._dragging = False
        if pos is None:
            x = y = None
            slide = 0
        else:
            x, y = pos
            slide = 12 if self.use_alpha else 0
            self.move(x, y + slide)
        if self.use_alpha:
            self.set_opacity(0.0)
        self.show_all()
        self.present_with_time(self._server_time())
        self._focus_entry()
        if pos is None:
            # "wm" mode: the window manager chose the monitor. Once it is placed, restore the
            # position the user last dragged the window to on that monitor (if any).
            if self.use_alpha:
                self.set_opacity(0.0)
            GLib.timeout_add(60, self._place_from_memory)
        else:
            self._animate(x, y, slide)
        GLib.timeout_add(250, self._ensure_focus)

    def _ensure_focus(self) -> bool:
        if self.get_visible() and not self.is_active():  # focus-stealing prevention: try once more
            self.present_with_time(self._server_time())
            self._focus_entry()
        return False

    def _animate(self, x: int, y: int, slide: int) -> None:
        """Fade in + slide up over ~150 ms."""
        if self._anim_id:
            GLib.source_remove(self._anim_id)
            self._anim_id = 0
        if not self.use_alpha:
            return
        start = time.monotonic()

        def tick() -> bool:
            t = min(1.0, (time.monotonic() - start) / 0.15)
            e = 1 - (1 - t) ** 3  # ease-out cubic
            self.set_opacity(e)
            if x is not None:
                self.move(x, y + int(round(slide * (1 - e))))
            if t >= 1.0:
                self._anim_id = 0
                return False
            return True

        self._anim_id = GLib.timeout_add(12, tick)

    def hide_popup(self) -> None:
        if self.get_visible() and self._user_moved:
            self._save_position()
            self._user_moved = False
        self._dragging = False
        if self._anim_id:
            GLib.source_remove(self._anim_id)
            self._anim_id = 0
        if self._open_popover is not None:
            self._open_popover.popdown()
        self.hide()
        if self.use_alpha:
            self.set_opacity(1.0)

    def toggle(self) -> None:
        if self.get_visible():
            self.hide_popup()
        else:
            self.show_popup()

    def refresh(self) -> None:
        """Called when new items arrive while the popup is open."""
        if self.get_visible() and self.active_tab == "clipboard":
            item = self._selected_item()
            self._rebuild(select_id=item.id if item else None)


# --------------------------------------------------------------------------- #
# SocketServer - single-instance command channel
# --------------------------------------------------------------------------- #
class SocketServer:
    """Listens on a Unix socket; integrated in the GLib main loop (no threads)."""

    def __init__(self, path: Path, handler: Callable[[str], str]) -> None:
        self.path, self.handler = path, handler
        self.sock: Optional[socket.socket] = None
        self._watch = 0

    def start(self) -> None:
        # The flock in run_service() guarantees no other live instance: a leftover file is stale.
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        os.chmod(self.path, 0o600)
        s.listen(8)
        s.setblocking(False)
        self.sock = s
        self._watch = GLib.io_add_watch(s.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, self._on_ready)
        log.info("Listening on %s", self.path)

    @guarded(True)
    def _on_ready(self, fd: int, cond: "GLib.IOCondition") -> bool:
        assert self.sock is not None
        try:
            conn, _ = self.sock.accept()
        except (BlockingIOError, InterruptedError):
            return True
        try:
            conn.settimeout(0.5)
            cmd = conn.recv(256).decode("utf-8", "replace").strip()
            try:
                reply = self.handler(cmd)
            except Exception:  # noqa: BLE001
                log.exception("command %r failed", cmd)
                reply = "error"
            conn.sendall((reply + "\n").encode())
        except OSError:
            pass
        finally:
            conn.close()
        return True

    def stop(self) -> None:
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if self.sock:
            self.sock.close()
            self.sock = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class Application:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = HistoryStore(DATA_DIR, cfg.max_items, cfg.max_image_mb * 1024 * 1024)
        self.theme = ThemeManager(cfg)
        self.theme.apply()
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.watcher = ClipboardWatcher(self.store, cfg, self._on_history_changed)
        self.popup = PopupWindow(self)
        self.server = SocketServer(SOCKET_PATH, self.handle_command)
        self._quitting = False

    # ---- lifecycle ---- #
    def run(self, show: bool) -> None:
        self.server.start()
        self.watcher.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if show:
            GLib.idle_add(self._ui, self.popup.show_popup)
        if self.cfg.check_updates and self.cfg.update_repo:
            GLib.timeout_add_seconds(int(os.environ.get("CLIPBOARD_HISTORY_UPDATE_DELAY", "120")), self._check_updates)
        log.info("Clipboard History %s running (pid %d)", __version__, os.getpid())
        Gtk.main()

    def _check_updates(self) -> bool:
        """Runs shortly after start and then daily. Network work happens in a thread."""
        def work() -> None:
            try:
                import updater
                info = updater.check(self.cfg.update_repo)
                if info["newer"]:
                    txt = updater.fmt(info["latest"])
                    GLib.idle_add(self._ui, lambda: self.popup.set_update_available(txt))
                    log.info("Update available: %s", txt)
            except Exception as e:  # noqa: BLE001 - offline etc. must never disturb the app
                log.info("Update check skipped: %s", e)
        threading.Thread(target=work, daemon=True).start()
        GLib.timeout_add_seconds(86400, self._check_updates)
        return False

    def _on_signal(self, *_: Any) -> bool:
        log.info("Signal received, shutting down")
        self.quit()
        return GLib.SOURCE_REMOVE

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        for fn in (self.watcher.stop, self.server.stop, self.store.close):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("error during shutdown")
        Gtk.main_quit()

    # ---- socket commands ---- #
    def handle_command(self, cmd: str) -> str:
        cmd = cmd.strip().lower()
        if cmd == "toggle":
            GLib.idle_add(self._ui, self.popup.toggle)
        elif cmd == "show":
            GLib.idle_add(self._ui, self.popup.show_popup)
        elif cmd == "hide":
            GLib.idle_add(self._ui, self.popup.hide_popup)
        elif cmd == "quit":
            GLib.idle_add(self._ui, self.quit)
        elif cmd in ("status", "ping"):
            return "ok"
        else:
            return "unknown command"
        return "ok"

    @staticmethod
    def _ui(fn: Callable[[], None]) -> bool:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("UI command failed")
        return False

    # ---- clipboard writes ---- #
    def _on_history_changed(self) -> None:
        self.popup.refresh()

    def copy_item(self, item_id: int) -> bool:
        row = self.store.get_full(item_id)
        if row is None:
            return False
        if row["kind"] == "text":
            self.watcher.mark_own(row["hash"])
            self.clipboard.set_text(row["content"], -1)
        else:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(str(self.store.image_path(row["content"])))
            except GLib.Error:
                log.warning("Image file missing for item %s; removing entry", item_id)
                self.store.delete(item_id)
                return False
            self.watcher.mark_own(row["hash"])
            self.clipboard.set_image(pb)
        self.store.touch(item_id)  # re-copied items move to the top
        return True

    def copy_text(self, text: str) -> None:
        norm = normalize_text(text, self.cfg.max_text_bytes)
        self.watcher.mark_own(norm[1] if norm else None)
        self.clipboard.set_text(text, -1)

    def schedule_auto_paste(self) -> None:
        """Optional: send Ctrl+V to whatever window regains focus (needs xdotool or wtype)."""
        wayland = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        if wayland and shutil.which("wtype"):
            cmd = ["wtype", "-M", "ctrl", "v", "-m", "ctrl"]
        elif shutil.which("xdotool"):
            cmd = ["xdotool", "key", "ctrl+v"]
        elif shutil.which("wtype"):
            cmd = ["wtype", "-M", "ctrl", "v", "-m", "ctrl"]
        else:
            log.warning("auto_paste is on but neither xdotool nor wtype is installed")
            return

        def run() -> bool:
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                log.exception("auto-paste failed")
            return False

        GLib.timeout_add(200, run)  # give the previous window time to regain focus


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def acquire_lock() -> Optional[int]:
    """Exclusive, non-blocking flock: guarantees a single running instance."""
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def run_service(show: bool, debug: bool) -> int:
    setup_logging(debug)
    lock_fd = acquire_lock()
    if lock_fd is None:
        # Another instance holds the lock (running, or still starting): hand the command over.
        cmd = ARGS.command or "ping"
        for _ in range(20):
            reply = send_command(cmd, 1.0)
            if reply is not None:
                print(reply)
                return 0
            time.sleep(0.25)
        print("clipboard-history: another instance holds the lock but does not respond", file=sys.stderr)
        return 1
    if Gdk.Display.get_default() is None:
        msg = "no display available (is DISPLAY / WAYLAND_DISPLAY set?)"
        log.error(msg)
        print(f"clipboard-history: {msg}", file=sys.stderr)
        return 1
    try:
        app = Application(load_config())
        app.run(show)
    except Exception:  # noqa: BLE001
        log.exception("Fatal error")
        return 1
    finally:
        os.close(lock_fd)
    return 0


def main() -> int:
    return run_service(show=ARGS.command in ("toggle", "show"), debug=ARGS.debug)


if __name__ == "__main__":
    sys.exit(main())
