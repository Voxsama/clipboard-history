# Clipboard History

A Windows 11 (Win+V) style clipboard history for Linux: GTK 3 popup, text + image history, pinning,
search, emoji picker. Works on GNOME Wayland (via XWayland) and X11. Everything stays local.

## Files

```
clipboard-history/
├── clipboard_history.py   # the app (ClipboardWatcher, HistoryStore, PopupWindow, SocketServer, ...)
├── emoji_data.py          # embedded emoji list (~230 emojis, 7 categories)
├── style.css              # GTK CSS; colours come from @define-color palettes
├── install.sh
├── uninstall.sh
└── README.md
```

## Dependencies

| Purpose | Package | Install |
|---|---|---|
| Python 3.10+, GTK 3, PyGObject (required) | `python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0` | `sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0` |
| Symbolic icons (usually present) | `adwaita-icon-theme` | `sudo apt install adwaita-icon-theme` |
| Optional `wl-paste --watch` backend | `wl-clipboard` | `sudo apt install wl-clipboard` |
| Optional auto-paste on X11 | `xdotool` | `sudo apt install xdotool` |
| Optional auto-paste on Wayland | `wtype` | `sudo apt install wtype` |

No pip packages are needed (SQLite comes with Python). `install.sh` runs the apt commands for you.

## Install

```bash
cd clipboard-history
chmod +x install.sh uninstall.sh
./install.sh
```

The installer copies the app to `~/.local/share/clipboard-history/`, creates `~/.local/bin/clipboard-history`,
adds `~/.config/autostart/clipboard-history.desktop` (starts at login) and starts the service.

## Usage

```
clipboard-history            # start the background service (the autostart entry does this)
clipboard-history --toggle   # show/hide the popup (starts the service first if needed)
clipboard-history --show | --hide | --status | --quit
clipboard-history --debug    # run in the foreground with verbose logging
```

| Key | Action |
|---|---|
| type | filter (search box is focused when the popup opens) |
| ↑ / ↓ (PgUp / PgDn) | move selection |
| Enter | copy the selected item and close |
| Esc | close (or close an open "..." menu) |
| Delete | delete the selected item (if the search box has text and the cursor is not at its end, Delete edits the text instead) |
| Ctrl+P | pin / unpin |
| Ctrl+Tab | switch Clipboard / Emoji tab |

Mouse: click a card to copy it and close. Hover a card and use "..." (or right-click) for Pin/Unpin and Delete.
"Clear all" keeps pinned items unless you confirm deleting them too. Clicking outside the popup closes it.

## Bind Super+V

Wayland does not let applications grab global keys, so the desktop itself runs `clipboard-history --toggle`.

### GNOME / Zorin OS

1. **Free Super+V first.** GNOME uses Super+V for the notification list.
   *Settings → Keyboard → Keyboard Shortcuts → View and Customize Shortcuts → System (or Notifications)* →
   *"Show the notification list"* → click it, press **Backspace** to clear it (or "Disable").
   If another custom shortcut uses Super+V, delete or change that one too.
2. Go to *Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts* and click **+**.
3. Name: `Clipboard history`
   Command: `/home/YOUR_USER/.local/bin/clipboard-history --toggle`
   (Use the full path: shortcut commands do not expand `~` and `~/.local/bin` may not be in their PATH.)
4. Click *Set Shortcut…* and press **Super+V**. Click **Add**.

CLI alternative:

```bash
KB=/org/gnome/settings-daemon/plugins/media-keys
gsettings set org.gnome.shell.keybindings toggle-message-tray "[]"      # free Super+V
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['$KB/custom-keybindings/clipboard/']"                               # NOTE: overwrites existing custom shortcuts, append instead if you have some
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KB/custom-keybindings/clipboard/ name 'Clipboard history'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KB/custom-keybindings/clipboard/ command "$HOME/.local/bin/clipboard-history --toggle"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KB/custom-keybindings/clipboard/ binding '<Super>v'
```

### i3

```
bindsym $mod+v exec clipboard-history --toggle
for_window [class="clipboard-history"] floating enable
```

(The window's WM class is `clipboard-history`, so the rule matches.)

## Configuration

`~/.config/clipboard-history/config.json` is created on first start:

```json
{
  "max_items": 100,
  "auto_paste": false,
  "theme": "auto",
  "popup_position": "cursor",
  "max_text_bytes": 1048576,
  "max_image_mb": 25,
  "use_wl_paste": true,
  "poll_interval_ms": 500
}
```

* `max_items` – history size; oldest **unpinned** items are dropped first, pinned items are never auto-deleted.
* `auto_paste` – after choosing an item, send Ctrl+V (`xdotool key ctrl+v` on X11, `wtype -M ctrl v` on Wayland) if installed.
* `theme` – `auto` (follow system, dark if unknown), `dark`, `light`.
* `popup_position` – `cursor` or `center`.

Restart after editing: `clipboard-history --quit; clipboard-history --daemon &`.

Data: `~/.local/share/clipboard-history/` (`history.db`, `images/`, `images/thumbs/`, `app.log`).
Customise the look by editing `style.css` and the `DARK_PALETTE` / `LIGHT_PALETTE` dicts (`@define-color`s).

## Uninstall

```bash
./uninstall.sh                  # asks whether to keep the history
./uninstall.sh --keep-history
./uninstall.sh --delete-history
```

## Troubleshooting

**The window doesn't appear**
* `clipboard-history --status` should print `ok`. If it says "not running": `clipboard-history --debug` and read the output;
  also see `~/.local/share/clipboard-history/app.log`.
* "PyGObject/GTK 3 is missing" → run the apt command from *Dependencies*. Use the system `python3`, not a venv/conda Python.
* Test without the shortcut: run `clipboard-history --toggle` in a terminal. If that works, the shortcut is the problem.
* Popup shows but is off in a corner on GNOME Wayland: XWayland cannot always see the pointer. Set `"popup_position": "center"`.
* Popup appears but has no keyboard focus / closes immediately: GNOME's focus-stealing prevention can interfere; make sure you
  trigger it from the keyboard shortcut and not from a delayed command. Check `app.log`.
* No transparency / solid background: no compositor is running (normal on some X11 setups); the app falls back to a solid colour.

**Clipboard not captured**
* The app must run through XWayland; it sets `GDK_BACKEND=x11` itself. Check `echo $DISPLAY` is non-empty in your session
  and that XWayland is installed (`sudo apt install xwayland`).
* Copy something, then look at `app.log` (run with `--debug` for details). Items from password managers (with the
  `x-kde-passwordManagerHint` flag) are deliberately ignored.
* Only text and images (PNG-convertible) are captured; files copied in a file manager are stored as their text paths.
* On wlroots/KDE compositors install `wl-clipboard` for the extra `wl-paste --watch` backend. On GNOME it is not usable (Mutter lacks the
  needed protocol); the log says so, and the GTK backend is used, with a 500 ms polling fallback.
* Text above 1 MB is truncated when stored (`max_text_bytes`). Images above `max_image_mb` are ignored.

**Shortcut not working**
* Super+V is still bound to "Show the notification list" — clear it (see *Bind Super+V*, step 1).
* Use the absolute path in the custom shortcut command. Run that exact command in a terminal to verify.
* `ls $XDG_RUNTIME_DIR/clipboard-history.*` should list `.sock` and `.lock` while the service runs. If a crashed instance left junk behind,
  `pkill -f clipboard_history.py` and start again; stale sockets are replaced automatically.
* The Super key alone may be bound to the Activities overview; that does not conflict with Super+V.

## Updating from GitHub

Updates come from the tags (versions) of your GitHub repository.

One-time setup (use your own GitHub name):

```
clipboard-history --set-update-repo YOUR-USERNAME/clipboard-history
```

Then:

```
clipboard-history --check-update   # is there a newer version?
clipboard-history --update         # download, install, restart
clipboard-history --rollback       # go back to the version before the last update
```

Once the repository is set, the app also checks once a day. If a newer version exists, an
**Update** button appears at the top of the popup, next to "Clear all". Click it and the app
updates itself and restarts.

Your history, pinned items and settings are never touched by an update. The previous version is
kept in `~/.local/share/clipboard-history/previous`, and if a new version fails to start it is
rolled back automatically. To stop the daily check, set `"check_updates": false` in
`~/.config/clipboard-history/config.json`.

### Publishing a new version (for the author)

1. Change `__version__` in `clipboard_history.py` (for example to `"2.1.0"`).
2. `git add . && git commit -m "Version 2.1"`
3. `git tag v2.1 && git push && git push origin v2.1`

The tag and `__version__` must match, otherwise the updater refuses the update and says why.
The repository must be public for other computers to download updates without logging in.
