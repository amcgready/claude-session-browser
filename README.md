<div align="center">

<img src="docs/logo.png" width="120" alt="Claude Session Browser">

# Claude Session Browser

[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078d4)](https://github.com/juppeee/claude-session-browser/releases/latest)
[![macOS](https://img.shields.io/badge/macOS-10.13+-000000)](MACOS_PORT_GUIDE.md)
[![Python](https://img.shields.io/badge/Python-3.11-3776ab)](https://www.python.org/)
[![UI](https://img.shields.io/badge/UI-pywebview-ec7456)](https://pywebview.flowrl.com/)
[![License](https://img.shields.io/badge/License-MIT-3ecf8e)](LICENSE)
[![Release](https://img.shields.io/github/v/release/juppeee/claude-session-browser?color=ffb454)](https://github.com/juppeee/claude-session-browser/releases/latest)

**Every Claude Code session you ever started, in one window — search them, and double-click one to jump straight back in.**

<a href="https://github.com/juppeee/claude-session-browser/releases/latest/download/ClaudeSessionBrowser-Setup.exe"><img src="https://img.shields.io/badge/Download-Installer%20for%20Windows-ec7456?style=for-the-badge&logo=windows&logoColor=white" alt="Download the installer for Windows"></a>

<sub>Installs per user — no admin rights, no UAC prompt, and it never touches `~/.claude`</sub>

[Quick start](#quick-start) · [What you get](#what-you-get) · [Clawd](#clawd-your-desktop-buddy) · [Clawdmeter](#clawdmeter) · [Settings](#settings) · [Uninstall](#updating-and-uninstalling) · [Credits](#credits)

</div>

---

Claude Code keeps every session on disk under `~/.claude/projects`, but getting
back into one means digging out a session ID and typing `claude --resume`. This
app lists them all — title, folder, message count, when you last touched it —
and puts you back into one with a double-click.

<div align="center">

<img src="docs/screenshot-sessions.png" width="880" alt="The session list with the detail panel open">

<sub>Pick a session and everything about it is on the right — folder, message counts, and how it started</sub>

</div>

## What you get

- **Every session in one list** — Claude's auto-title or your own, folder, message count, last activity
- **Find it fast** — live search across title, folder, ID and first question; sortable, configurable columns
- **Make it yours** — colour-code sessions, rename them for good, copy the ID
- **One click back in** — opens Terminal.app (macOS) or Windows Terminal/`cmd` (Windows) with the session resumed
- **Know where your quota stands** — 5-hour and weekly usage with a live countdown to the reset
- **Get told, not surprised** — a heads-up before the limit is full, and a notification when it resets
- **[Clawd](#clawd-your-desktop-buddy)** — a 20×20 pixel buddy on your desktop who acts out what Claude is doing
- **[Clawdmeter](#clawdmeter) support** — mirror Clawd onto a real device over Bluetooth (Windows)
- **German and English** language support
- **Updates itself** from GitHub

## Quick start

### Windows
**[⬇ Download ClaudeSessionBrowser-Setup.exe](https://github.com/juppeee/claude-session-browser/releases/latest/download/ClaudeSessionBrowser-Setup.exe)** and run it. That's the whole installation.

It installs per user, so **no admin rights and no UAC prompt**, and it never
touches `~/.claude` — your sessions and settings are none of the installer's
business. The app starts by itself when the installer finishes.

> **First launch:** Windows may show a SmartScreen warning ("unknown publisher")
> because the app isn't code-signed. Click **More info → Run anyway**. It won't
> ask again — the installed copy carries no "downloaded from the web" mark.

### macOS
For detailed setup instructions, see [MACOS_PORT_GUIDE.md](MACOS_PORT_GUIDE.md).

Quick start:
```bash
pip install pywebview
python3 claude_sessions.py
```

### Run from source (all platforms)

```bash
git clone https://github.com/juppeee/claude-session-browser.git
cd claude-session-browser
pip install pywebview
python3 claude_sessions.py
```

**Windows only** — additional packages for tray icon and Clawdmeter:

```bash
pip install pystray Pillow bleak
```

<details>
<summary><b>Build your own installer (Windows)</b></summary>

```bash
pip install pyinstaller
winget install JRSoftware.InnoSetup
build.bat
```

Three files land in `dist\`: the installer, a standalone one-file exe, and the
separate updater.

</details>

## Clawd, your desktop buddy

Clawd is a tiny animated character who sits on your desktop and shows what
Claude Code is up to — thinking, writing code, waiting for permission, out of
quota. Fifteen animations, chosen from what is actually happening in your
sessions rather than from a timer.

<div align="center">

<img src="docs/clawd-idle.png" height="120" alt="Clawd idle"> <img src="docs/clawd-thinking.png" height="120" alt="Clawd thinking"> <img src="docs/clawd-coding.png" height="120" alt="Clawd writing code"> <img src="docs/clawd-limit.png" height="120" alt="Clawd out of quota">

<sub>Waiting · thinking · writing code · out of quota</sub>

</div>

He comes with a frame or without. Turn the frame off and the backdrop goes with
it — what's left is just Clawd, floating on your desktop:

<div align="center">

<img src="docs/clawd-sleeping.png" height="100" alt="Clawd asleep, no frame"> <img src="docs/clawd-desk.png" height="100" alt="Clawd at his desk, no frame">

</div>

Switch him on in the **Buddy** tab. He can be there all the time or only while
Claude Code is running. Drag him wherever you like; right-click sends him away
for a while, and he returns with your next Claude Code terminal.

**Spotting permission prompts reliably.** Claude Code writes its question to the
terminal, never to the transcript, so the app has to infer it — and inference
goes wrong as soon as several terminals are open: one is working, another is
asking, and the window title belongs to the window, not the tab.

Turn on *Spot permission prompts reliably* under **Settings → Connections** and
Claude Code reports it itself. That adds a single hook to
`~/.claude/settings.json`; your other hooks stay untouched, and it applies to
sessions you start afterwards.

## Clawdmeter

*Optional, Windows only, and someone else's project — skip this if you don't own the device or aren't on Windows.*

The [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) is a small ESP32
device by [Hermann Björgvin](https://github.com/HermannBjorgvin) that displays
your Claude usage. This app speaks to it over Bluetooth on Windows and can
mirror Clawd onto it, so the device acts out the same state your desktop buddy
does, rather than only reacting to how fast your quota is burning. It reports
its battery level back, and warns you before it runs flat.

<div align="center">

<img src="docs/clawdmeter-case.png" width="330" alt="Clawdmeter in a printed case, showing usage and the buddy"> <img src="docs/clawdmeter-desk.png" width="330" alt="Clawdmeter on a desk next to a keyboard">

<sub>Renders of the printable case — usage on one screen, Clawd on the other</sub>

</div>

**Windows:** Pair the device once in the Windows Bluetooth settings, then enable it under
**Settings → Connections**.

**Stock firmware is enough for most of it.** Usage and battery need nothing
special — flash Hermann's firmware as usual and the device shows your quota
while picking its own animations, exactly as it does without this app.

Mirroring Clawd onto it needs two things his firmware doesn't carry yet: a
field in the BLE payload so a host can name an animation, and sprites for the
states it doesn't know — waiting for permission, out of quota. Until those land
upstream they live in a [fork of his firmware](https://github.com/juppeee/Clawdmeter/tree/csb-buddy);
flash that branch and the device acts out what Claude is doing.

**Want a case for it?** The STL files are on
[MakerWorld](https://makerworld.com/de/@Juppi187) — print one and your
Clawdmeter gets a body to match the buddy.

See [Credits](#credits) for who built what.

## Settings

1. Open the **Settings** tab
2. Point **Sessions folder** at your Claude projects directory — it is found automatically, but you can override it
3. Pick colours, columns and language under **Appearance**
4. Choose how sessions open under **Connections**

| Setting | Default | What it does |
|---|---|---|
| Language | Automatic | German on German systems, English everywhere else |
| Open with | Automatic | Terminal.app (macOS), Windows Terminal, or `cmd` (Windows) |
| Claude command | `claude` | Path or name of the Claude CLI |
| Keep running in background | On (Windows) / Off (macOS) | The X button hides the app in the system tray (Windows only) |
| Start with Windows | On (Windows) / Off (macOS) | Registry entry under `HKCU\Run` (Windows only) |
| Notify on limit reset | On | A notification when your quota is back |
| Warn before the limit is full | On, at 90% | Once per 5-hour window |
| Clawdmeter battery warning | On, at 15% | Once per discharge (Windows only) |

<details>
<summary><b>Where your data lives</b></summary>

| Path | Contents |
|---|---|
| `~/.claude/projects/` | Your Claude Code sessions — read only, never modified |
| `~/.claude/session_browser_settings.json` | This app's settings |
| `~/.claude/session_titles.json` | Titles you renamed yourself |
| `~/.claude/settings.json` | Claude Code's own settings — only touched if you enable the hook |
| `~/.claude/csb_hooks/` | What the hook reports, one small file per session |

</details>

<details>
<summary><b>How the state detection works</b></summary>

The app tails the newest session transcript and works out what Claude is doing
from the last few entries: a `thinking` block, a text response, a tool call with
no result yet, an `end_turn`, or an API error carrying a rate-limit status. That
state decides which animation Clawd plays and what the Clawdmeter shows.

Two things are deliberately not read from the transcript, because they aren't in
it: the permission prompt (see [above](#clawd-your-desktop-buddy)), and your
exact quota, which comes from the API rate-limit headers instead.

</details>

<details>
<summary><b>Under the hood</b></summary>

Clawd's animations are 20×20 pixel sprites with a 10-colour palette, packed into
`clawd_sprites.py` by `pack_sprites.py`.

Interface text lives in `i18n.py`, where the German sentence is the key — a
missing translation shows German rather than an empty label.
`tools/check_i18n.py` verifies every string has an English version and that
placeholders match on both sides; it runs as the first step of every build.

</details>

<details>
<summary><b>Publishing a release</b> (maintainer)</summary>

1. Raise `VERSION` in `claude_sessions.py`
2. Run `build.bat` and attach the installer and the one-file exe to a GitHub release
3. Update `version.json` with the same version and a short note, then push

The app compares its own `VERSION` against `version.json` in this repo on start.

</details>

## Updating and uninstalling

The app checks GitHub for updates by itself and offers to install them. No
internet, no problem — the check is skipped silently.

### Windows
**Settings → Apps → Claude Session Browser → Uninstall**

### macOS
Delete the app from Applications folder, or use `pip uninstall` if installed via pip.

**All platforms:** Your sessions, titles and settings under `~/.claude` survive. Delete
`session_browser_settings.json` and `session_titles.json` by hand if you want
those gone too.

## Credits

**The Clawdmeter is not this project's work.** The device and its firmware are the work of [Hermann Björgvin](https://github.com/HermannBjorgvin/Clawdmeter) — the hardware abstraction, five board ports, the LVGL interface, the BLE service and the animation engine are all his.

Talking to it is one feature of this app among many. The Session Browser is first and foremost a browser for your Claude Code sessions: it searches them, colour-codes them and puts you back into one with a double-click, and it does all of that without a Clawdmeter anywhere in sight.

**Clawd himself** comes from [claudepix](https://claudepix.vercel.app) by [@amaanbuilds](https://x.com/amaanbuilds), a library of pixel-art Clawd sprites — the same source Hermann's firmware draws on. Some of the animations here were taken from there, others inspired by it, and nearly all have been reworked or redrawn since. Go and have a look, it's where Clawd got his face.

What this project adds on top of Hermann's work is two things: the Bluetooth connection for Windows (his daemon is a Linux shell script built on bluez), and **activity-driven animations**. Upstream picks an animation from how fast your quota is burning — a rate measured over a six-sample ring buffer and grouped into calm / normal / active / heavy. It cannot know *what* Claude is doing. The Session Browser reads the session transcripts, works out the actual state — thinking, writing code, waiting for permission, out of quota — and tells the device which animation to show. Turn that off and the device falls back to Hermann's usage groups.

## License

The **code** in this repository is MIT — see [LICENSE](LICENSE).

That covers the code and nothing else. Clawd is Anthropic's mascot, and the
sprites in `clawd_sprites.py` are him; they started at
[claudepix](https://claudepix.vercel.app) and were reworked from there. The
artwork isn't mine to license, so the MIT grant doesn't reach it. The Clawdmeter
repository carries the same note.
