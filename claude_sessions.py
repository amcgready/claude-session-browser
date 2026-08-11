#!/usr/bin/env python3
"""
Claude Session Browser  (pywebview-Edition)
===========================================
Modernes natives Fenster (HTML/CSS/JS-Oberflaeche, Python-Backend) zum
Durchsuchen aller lokalen Claude-Code-Sessions und Wiedereinstieg per Klick.

Start:   python claude_sessions.py
Bauen:   pyinstaller --onefile --noconsole --name ClaudeSessionBrowser \
                      --icon claude_sessions.ico \
                      --add-data "logo.png;." claude_sessions.py
"""

import os
import re
import sys
import ssl
import json
import time
import zlib
import queue
import shutil
import base64
import ctypes
import logging
import tempfile
import threading
import webbrowser
import datetime as dt
import subprocess
import urllib.request

import webview

import i18n
from i18n import t

# Nur damit PyInstaller die Tcl/Tk-Daten mit-buendelt (der eigentliche Import
# passiert lazy im BuddyController-Thread).
try:
    import tkinter as _tk_probe  # noqa: F401
    import _tkinter as _tkc_probe  # noqa: F401
except Exception:
    pass

# pywebview-Introspektions-Geschwaetz daempfen (harmlose COM-/Rekursionswarnungen)
logging.getLogger("pywebview").setLevel(logging.CRITICAL)

# ----- Version & Update ---------------------------------------------------- #
VERSION = "1.4.0"
# Wird beim GitHub-Setup auf dein echtes Repo gesetzt (OWNER/REPO):
UPDATE_URL = "https://raw.githubusercontent.com/juppeee/claude-session-browser/main/version.json"


def _vtuple(v):
    out = []
    for p in str(v).lstrip("vV").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


# --------------------------------------------------------------------------- #
#  Pfade / Hilfen
# --------------------------------------------------------------------------- #
HOME = os.path.expanduser("~")
TITLES_FILE = os.path.join(HOME, ".claude", "session_titles.json")
SETTINGS_FILE = os.path.join(HOME, ".claude", "session_browser_settings.json")
CLAUDE_SETTINGS_FILE = os.path.join(HOME, ".claude", "settings.json")

# Hier legt der Hook ab, was Claude Code gerade meldet - eine Datei je
# Session. Siehe _hook_entry().
HOOK_DIR = os.path.join(HOME, ".claude", "csb_hooks")
HOOK_FRESH_S = 300.0        # aeltere Meldungen gelten als abgestanden


def _resource(name):
    """Pfad zu mitgelieferter Datei – als .py (neben dem Script) und als
    gebaute .exe (PyInstaller entpackt nach sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def norm(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


# --------------------------------------------------------------------------- #
#  Hook: Claude Code meldet selbst, wenn es auf eine Antwort wartet
# --------------------------------------------------------------------------- #
# Ohne Hook muss die App raten, ob ein Werkzeug noch laeuft oder ob die
# Rueckfrage im Terminal steht - Claude Code schreibt sie nur dorthin, nicht
# ins Protokoll. Geraten wird ueber die Laufanzeige im Fenstertitel, und das
# geht schief, sobald mehrere Terminals offen sind: eines arbeitet, das
# andere fragt, und der Titel gehoert dem Fenster, nicht dem Reiter.
#
# Der Hook beseitigt das Raten. Eingehaengt wird bewusst nur "Notification" -
# das ist genau der Moment, in dem Claude Code auf dich wartet. Haeufige
# Ereignisse wie PreToolUse waeren jedes Mal ein Prozessstart und wuerden
# Claude bei jedem Werkzeugaufruf ausbremsen.
def _hook_entry():
    """Laeuft als eigener, kurzlebiger Prozess: Claude Code ruft die App mit
    --csb-hook auf und schickt die Meldung als JSON auf die Standardeingabe."""
    try:
        roh = sys.stdin.read()
    except Exception:
        return
    try:
        d = json.loads(roh) if roh.strip() else {}
    except (ValueError, TypeError):
        d = {}
    sid = str(d.get("session_id") or "unbekannt")
    # Dateinamen absichern - die Kennung kommt von aussen und landet in einem
    # Pfad. Alles ausser dem, was eine Session-ID sein darf, fliegt raus.
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:64] or "unbekannt"
    try:
        os.makedirs(HOOK_DIR, exist_ok=True)
        with open(os.path.join(HOOK_DIR, sid + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"ereignis": d.get("hook_event_name") or "Notification",
                       "meldung": str(d.get("message") or "")[:300],
                       "zeit": time.time()}, fh)
    except OSError:
        return
    # Alte Dateien wegraeumen: jede Session hinterlaesst eine, sonst waechst
    # der Ordner unbegrenzt.
    try:
        grenze = time.time() - 7 * 24 * 3600
        for name in os.listdir(HOOK_DIR):
            p = os.path.join(HOOK_DIR, name)
            try:
                if os.path.getmtime(p) < grenze:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _hook_wartet(session_id, seit):
    """Wartet Claude Code in dieser Session auf eine Antwort?

    None = keine Auskunft (kein Hook eingerichtet oder nichts gemeldet), dann
    bleibt es beim Raten. True/False = der Hook weiss es.

    Eine Meldung gilt nur, solange danach nichts mehr ins Protokoll
    geschrieben wurde: kam eine neue Zeile, ist die Frage beantwortet.
    """
    if not session_id:
        return None
    try:
        with open(os.path.join(HOOK_DIR, session_id + ".json"),
                  encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    zeit = float(d.get("zeit") or 0)
    if not zeit or time.time() - zeit > HOOK_FRESH_S:
        return None
    return zeit > (seit or 0)


# Woran wir unsere eigenen Eintraege in fremden Einstellungen wiedererkennen.
_HOOK_MARKE = "--csb-hook"


def _hook_command():
    """Der Befehl, den Claude Code aufrufen soll."""
    if getattr(sys, "frozen", False):
        return '"%s" %s' % (sys.executable, _HOOK_MARKE)
    return '"%s" "%s" %s' % (sys.executable,
                             os.path.abspath(__file__), _HOOK_MARKE)


def _claude_settings_lesen():
    try:
        with open(CLAUDE_SETTINGS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def hooks_eingerichtet():
    """Steht unser Hook in den Claude-Code-Einstellungen?"""
    gruppen = (_claude_settings_lesen().get("hooks") or {}).get("Notification")
    if not isinstance(gruppen, list):
        return False
    for g in gruppen:
        for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
            if isinstance(h, dict) and _HOOK_MARKE in str(h.get("command", "")):
                return True
    return False


def hooks_einrichten(an):
    """Traegt unseren Hook in ~/.claude/settings.json ein oder entfernt ihn.

    Fremde Hooks bleiben unangetastet - erkannt wird ausschliesslich der
    eigene Eintrag am Aufrufparameter. Die uebrige Datei wird eingelesen und
    unveraendert zurueckgeschrieben.
    """
    d = _claude_settings_lesen()
    hooks = d.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    gruppen = hooks.get("Notification")
    if not isinstance(gruppen, list):
        gruppen = []

    # Eigene Eintraege in jedem Fall erst raus - beim Einschalten kommt der
    # frische Pfad hinterher, beim Ausschalten bleibt es dabei. Das raeumt
    # nebenbei einen veralteten Pfad nach einem Update weg.
    sauber = []
    for g in gruppen:
        if not isinstance(g, dict):
            sauber.append(g)
            continue
        rest = [h for h in (g.get("hooks") or [])
                if not (isinstance(h, dict)
                        and _HOOK_MARKE in str(h.get("command", "")))]
        if rest:
            g = dict(g)
            g["hooks"] = rest
            sauber.append(g)
        elif not g.get("hooks"):
            sauber.append(g)

    if an:
        sauber.append({"matcher": "",
                       "hooks": [{"type": "command",
                                  "command": _hook_command()}]})

    if sauber:
        hooks["Notification"] = sauber
    else:
        hooks.pop("Notification", None)
    if hooks:
        d["hooks"] = hooks
    else:
        d.pop("hooks", None)

    try:
        os.makedirs(os.path.dirname(CLAUDE_SETTINGS_FILE), exist_ok=True)
        save_json(CLAUDE_SETTINGS_FILE, d)
    except OSError:
        return False
    return True


DEFAULT_SETTINGS = {
    "hide_home": False,
    "hidden_folders": [],
    "session_colors": {},
    "sort_col": "when",
    "sort_rev": True,
    "projects_dir": "",          # leer = automatisch suchen
    "accent": "#ec7456",         # Akzentfarbe der Oberflaeche (Koralle, passend zum Logo)
    "bg_base": "#4a3a30",        # Grundton -> daraus wird die Hintergrund-Palette abgeleitet (warm)
    "language": "auto",          # auto (= system language) | de | en
    "terminal": "auto",          # auto | wt (Windows) | cmd (Windows) | terminal (macOS)
    "claude_cmd": "claude",      # Befehl/Pfad zur Claude-CLI
    "columns": [                 # sichtbare Spalten + Reihenfolge
        {"key": "title", "on": True}, {"key": "project", "on": True},
        {"key": "msgs", "on": True}, {"key": "when", "on": True},
        {"key": "id", "on": False}, {"key": "first", "on": False},
    ],
    "win_w": 0, "win_h": 0,      # gemerkte Fenstergroesse (0 = noch nicht gesetzt)
    "win_x": None, "win_y": None,  # gemerkte Position
    "win_max": False,            # war das Fenster maximiert?
    "close_to_tray": True if _IS_WIN else False,  # X = App verstecken (Tray-Icon) statt beenden (Windows only)
    "autostart": True if _IS_WIN else False,  # Beim Windows-Start automatisch mitstarten (Windows only)
    "autostart_registered": False,  # Merker: Registry-Eintrag beim ersten Mal setzen (Windows only)
    "notify_limit_reset": True,  # Notification wenn Claude-Limit sich zurueckgesetzt hat
    "limit_reset_at": 0,         # Epoche wann Limit zurueckgesetzt wird (aus JSONL geparst, 0 = unbekannt)
    "limit_reset_notified_for": 0,  # Fuer welche limit_reset_at wurde schon benachrichtigt (verhindert Doppel-Feuer)
    "notify_limit_near": True,   # Warnen bevor das 5h-Limit voll ist
    "limit_warn_pct": 90,        # ab wieviel Prozent gewarnt wird
    "notify_clawd_battery": True,  # Warnen wenn der Clawdmeter leer wird
    "clawd_battery_pct": 15,       # ab wieviel Prozent Restladung
    "clawd_battery_warned": False,  # Sperre, damit es nicht dauernd meldet
    # --- intern, aus den Ratelimit-Headern der API gepflegt ---
    "limit_window_at": 0,        # Reset-Zeitpunkt des aktuellen 5h-Fensters (Epoche)
    "limit_window_peak": 0,      # hoechste Auslastung in diesem Fenster (Prozent)
    "limit_warned_for": 0,       # fuer welches Fenster schon gewarnt wurde
    "onboarded": False,          # Erst-Einrichtung schon durchlaufen?
    "onboarded_version": "",     # zuletzt gesehene Onboarding-Version (fuer Re-Onboarding nach Updates)
    "buddy": {                   # Clawd-Buddy: kleines animiertes Desktop-Maskottchen
        "enabled": False,
        "size": 4,               # Skalierung: 20 px * size -> tatsaechliche Kantenlaenge
        "visibility": "when_claude",  # "when_claude" | "always" | "when_window"
        "target_window": "",     # Titel-Substring (Kleinschreibung) fuer visibility=when_window
        "x": 200, "y": 200,      # gemerkte Position auf dem Desktop
        "opacity": 100,          # 20..100 (Prozent) – 100 = voll deckend
        "party": False,          # Party-Modus: nur Tanz-Animation
        "party_style": "bounce", # Party-Stil: "bounce" (hopsend) oder "sway" (schaukelnd)
        "frame": False,          # (legacy) duenner Rahmen um den Buddy
        "frame_style": "off",    # "off" | "line" | "webcam"
        "frame_color": "#ec7456",
        "frame_label": "CLAWD",
    },
    "clawdmeter": False,         # Usage-Werte per BLE ans Clawdmeter-Geraet schicken
    "clawdmeter_addr": "",       # gewaehltes Geraet (leer = automatisch suchen)
    # Zeigt das Geraet dieselbe Animation wie der Buddy? Aus = es waehlt selbst
    # nach Auslastung aus (Originalverhalten der Firmware).
    "clawdmeter_buddy": True,
}

# Wenn diese Konstante sich aendert, sehen bestehende Nutzer das Onboarding erneut
# (ohne dass ihre Einstellungen ueberschrieben werden – die Schritte zeigen die
# aktuellen Werte an, ein Klick auf "Weiter" ohne Aenderung laesst alles wie es ist).
ONBOARDING_VERSION = "1.0.12"

# --------------------------------------------------------------------------- #
#  Buddy (Clawd-Maskottchen) – Sprite-Daten + Steuerung
# --------------------------------------------------------------------------- #
# Komprimierte 20x20-Pixel-Sprites aus dem Clawdmeter-Projekt
# (zlib + base64). 14 Animationen, entpackt ~192 KB. Ausgelagert in ein
# eigenes Modul, weil der Blob 3 KB gross ist.
try:
    from clawd_sprites import BLOB as BUDDY_BLOB
except Exception:
    BUDDY_BLOB = ""

def _decode_buddy_anims():
    """Entpackt die eingebetteten Sprites zu einer Dict-Struktur:
       {name: {"palette": [10 hex-Farben], "frames": [[400 ints], ...]}}"""
    try:
        raw = zlib.decompress(base64.b64decode(BUDDY_BLOB))
        arr = json.loads(raw.decode("utf-8"))
        return {a["n"]: {"palette": a["p"], "frames": a["f"]} for a in arr}
    except Exception:
        return {}


BUDDY_ANIMS = _decode_buddy_anims()

# Mapping von "detektiertem Zustand" -> Animations-Name.
BUDDY_STATE_MAP = {
    "limit":    "limit",             # Rate/Usage-Limit erreicht (sauer!)
    "active":   "work coding",       # Claude schreibt gerade (mtime < 3 s)
    "thinking": "work think",        # kurz danach, wenn's noch zappelt
    "awaiting_approval": "allow",    # Claude will Tool-Use, braucht dein Y/N
    "waiting":  "done",              # Claude fertig, wartet auf User-Input
    "recent":   "idle blink",        # zwischendurch mal blinzeln
    "idle":     "idle breathe",      # entspannt atmen
    "sleep":    "expression sleep",  # lange nichts los -> schlaeft
    "none":     "idle look around",  # kein Claude installiert / kein Projekt
    "party":    "dance bounce",      # Party-Modus (bounce)
    "party_sway": "dance sway",      # Party-Modus (sway - sanfter)
    "surprise": "expression surprise",
    "wink":     "expression wink",   # Easter-Egg bei langem Maus-Hover
    # Alternate-Anims fuer Variety (werden alle ~30s durchgemischt)
    "active_alt":   "write",         # Rotation-Alternative zu work coding
    "thinking_alt": "think",         # Rotation-Alternative zu work think
}

# Sehr spezifische Muster in der neuesten .jsonl-Datei die eindeutig auf ein
# erreichtes Claude-Nutzungslimit hindeuten. Absichtlich streng gewaehlt
# damit normale Chat-Erwaehnungen von „rate limit" o.ae. NICHT triggern.
# Wie lange ein Werkzeug ohne Ergebnis laufen darf, bevor der Buddy es als
# Rueckfrage deutet. Grosszuegig gewaehlt: die meisten Aufrufe sind in
# Sekunden durch, eine echte Rueckfrage steht dagegen bis jemand antwortet.
APPROVAL_AFTER_S = 30.0


_BUSY_CACHE = {"t": 0.0, "v": False}


def _claude_is_busy():
    """True wenn irgendein Claude-Terminal gerade sichtbar arbeitet.

    Claude Code stellt dem Fenstertitel waehrend der Arbeit ein Braille-Zeichen
    als Laufanzeige voran (U+2800..U+28FF, z.B. '⠂ Mein Projekt'). Sobald
    Claude auf eine Eingabe oder eine Erlaubnis wartet, verschwindet es.

    Das ist das einzige Lebenszeichen von aussen: waehrend ein Werkzeug laeuft,
    schreibt Claude Code nichts ins Protokoll, und die Rueckfrage steht nur im
    Terminal. Cache 1,5 s - der Aufruf zaehlt alle Fenster durch.

    Vorsicht: der Titel gehoert zum Fenster, nicht zum Tab. Arbeitet Claude in
    einem Hintergrund-Tab, fehlt die Laufanzeige. Deshalb ist das hier nur ein
    zusaetzliches Indiz und nicht die alleinige Entscheidung.
    """
    now = time.time()
    if now - _BUSY_CACHE["t"] < 1.5:
        return _BUSY_CACHE["v"]
    _BUSY_CACHE["t"] = now
    busy = False
    try:
        for hwnd, title in _win_list_windows_hwnd():
            t = title.strip()
            if not t or "claude" not in t.lower():
                continue
            if 0x2800 <= ord(t[0]) <= 0x28FF:
                busy = True
                break
    except Exception:
        pass
    _BUSY_CACHE["v"] = busy
    return busy


def _line_age(obj):
    """Alter einer Protokollzeile in Sekunden. Ohne brauchbaren Zeitstempel
    0.0 - dann gilt sie als frisch, und im Zweifel heisst das "arbeitet"."""
    ts = obj.get("timestamp") if isinstance(obj, dict) else None
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - t).total_seconds())


_LIMIT_PATTERNS = re.compile(
    # Klare "erreicht"-Phrasen (5h / weekly / session / max)
    r"(?:you'?ve (?:reached|hit) your (?:5.?hour|weekly|daily|24.?hour|max|session) limit)"
    # Explizites "reached"
    r"|(?:(?:5.?hour|weekly|daily|24.?hour|session|usage) limit reached)"
    # "session limit · resets ..." wie in der Claude-CLI-Statuszeile
    r"|(?:session limit[^\n]{0,40}resets?)"
    # Ganz nah dran (>= 90% verbraucht) – triggert auch die Vorwarn-Phase
    r"|(?:used 9\d%[^\n]{0,20}session limit)"
    r"|(?:used 100%[^\n]{0,20}session limit)"
    # Claude Max Plan Limit
    r"|(?:claude max plan[^\n]{0,80}limit reached)"
    # API-Fehler die Claude oft bei Ueberlast/Rate-Limit wirft
    r"|(?:api error[^\n]{0,40}server error mid.?response)"
    r"|(?:api error[^\n]{0,40}overloaded)"
    r"|(?:\"type\":\s*\"overloaded_error\")"
    r"|(?:rate_limit_error)"
    r"|(?:429[^\n]{0,20}too many requests)"
    # Auth-Fehler (Token abgelaufen/widerrufen) – User kann nicht arbeiten
    r"|(?:please run /login)"
    r"|(?:401[^\n]{0,60}(?:oauth|access token|unauthori[sz]ed))"
    r"|(?:access token has been revoked)"
    r"|(?:\"type\":\s*\"authentication_error\")"
    r"|(?:invalid[_ ]api[_ ]key)"
    r"|(?:authentication[^\n]{0,20}failed)",
    re.IGNORECASE,
)


_RESET_HHMM_RE = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*"
    r"(?:\(([^)]+)\))?", re.IGNORECASE)
_RESET_REL_RE = re.compile(
    r"resets?\s+in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", re.IGNORECASE)


def _parse_reset_epoch(text):
    """Aus einem Text wie 'resets 2pm (Europe/Berlin)' oder 'resets in 3h 12m'
    versuchen, einen absoluten Unix-Timestamp fuer die Reset-Zeit zu bauen.
    Rueckgabe: epoch (float) oder 0.0 wenn nichts erkannt."""
    if not text:
        return 0.0
    m = _RESET_REL_RE.search(text)
    if m:
        h = int(m.group(1) or 0)
        mm = int(m.group(2) or 0)
        if h or mm:
            return time.time() + h * 3600 + mm * 60
    m = _RESET_HHMM_RE.search(text)
    if m:
        try:
            import datetime
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = (m.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            now = datetime.datetime.now()
            target = now.replace(hour=hour, minute=minute,
                                 second=0, microsecond=0)
            if target <= now:
                target = target + datetime.timedelta(days=1)
            return target.timestamp()
        except (ValueError, OverflowError):
            return 0.0
    return 0.0


def _latest_jsonl_status(projects_dir, max_files=200, tail_kb=8):
    """Liest die neuesten Zeilen der zuletzt geaenderten .jsonl-Datei und
    liefert einen detaillierten Status-Dict zurueck.

    Verbesserte Logik nach Codex-Analyse:
    - Limit-Detection: rueckwaerts scannen, nicht nur letzte Zeile
    - Allow-Detection: nur wenn tool_use OHNE folgendes tool_result
    - Interne States fuer stabilere Animation-Zuordnung
    """
    empty = {
        "internal_state": "no_session",  # Neuer interner State
        "is_limit": False,
        "limit_type": None,  # "rate_limited" | "auth_required" | "api_overloaded"
        "reset_at": 0.0,
        "waiting": False,
        "awaiting_approval": False,
        "last_block_type": None,
        "last_tool_name": None,
        "has_pending_tool": False,
    }
    if not projects_dir or not os.path.isdir(projects_dir):
        return empty
    newest_path = None
    newest_mtime = 0.0
    count = 0
    try:
        for entry in os.scandir(projects_dir):
            if not entry.is_dir():
                continue
            try:
                for sub in os.scandir(entry.path):
                    if sub.is_file() and sub.name.endswith(".jsonl"):
                        try:
                            m = sub.stat().st_mtime
                            if m > newest_mtime:
                                newest_mtime = m
                                newest_path = sub.path
                        except OSError:
                            pass
                        count += 1
                        if count >= max_files:
                            break
            except OSError:
                pass
            if count >= max_files:
                break
    except OSError:
        pass
    if not newest_path:
        return empty

    try:
        with open(newest_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - tail_kb * 1024)
            fh.seek(start)
            if start > 0:
                fh.readline()
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace")
    except OSError:
        return empty

    def _classify_error(obj, line_text):
        """Klassifiziert einen Fehler in: rate_limited, auth_required, api_overloaded, oder None."""
        if not isinstance(obj, dict):
            return None
        # Strukturelle Fehler-Marker pruefen
        typ = obj.get("type")
        is_struct_error = typ in ("error", "tool_use_error", "system_error")
        is_struct_error = is_struct_error or obj.get("isError") is True
        is_struct_error = is_struct_error or obj.get("is_error") is True
        is_struct_error = is_struct_error or obj.get("isApiError") is True
        # Die echte Limit-Meldung von Claude Code sieht so aus:
        #   type="assistant", isApiErrorMessage=True, apiErrorStatus=429,
        #   error="rate_limit", Text "You've hit your session limit · resets ..."
        # Bisher wurde nur auf isApiError geprueft - ohne "Message". Wegen
        # dieses einen Wortes galt die Zeile nie als Fehler, und der Buddy
        # blieb beim Limit ahnungslos.
        is_struct_error = is_struct_error or obj.get("isApiErrorMessage") is True
        status_code = obj.get("apiErrorStatus")
        if isinstance(status_code, int):
            is_struct_error = True
        msg = obj.get("message")
        if isinstance(msg, dict):
            sr = msg.get("stop_reason")
            if sr in ("error", "rate_limited", "overloaded", "authentication_error"):
                is_struct_error = True
            content = msg.get("content")
            if isinstance(content, list):
                for it in content:
                    if isinstance(it, dict):
                        if it.get("is_error") or it.get("isError"):
                            is_struct_error = True
                        if it.get("type") in ("tool_use_error", "error"):
                            is_struct_error = True
        # `error` kommt mal als Objekt, mal als blosser Text ("rate_limit").
        err = obj.get("error")
        err_name = ""
        if isinstance(err, dict) and err.get("type"):
            is_struct_error = True
            err_name = str(err.get("type")).lower()
        elif isinstance(err, str) and err:
            is_struct_error = True
            err_name = err.lower()
        if not is_struct_error:
            return None

        # Eindeutige Signale zuerst - die haengen nicht am Wortlaut und
        # ueberleben eine geaenderte Formulierung.
        if status_code == 429 or "rate_limit" in err_name:
            return "rate_limited"
        if status_code == 401 or "auth" in err_name:
            return "auth_required"
        if status_code in (500, 502, 503, 529) or "overload" in err_name:
            return "api_overloaded"

        # Jetzt klassifizieren basierend auf Text-Patterns
        lt = line_text.lower()
        if "authentication" in lt or "auth" in lt and "error" in lt:
            return "auth_required"
        if obj.get("apiErrorType") == "overloaded" or "overloaded" in lt:
            return "api_overloaded"
        if _LIMIT_PATTERNS.search(line_text):
            return "rate_limited"
        if "rate" in lt and "limit" in lt:
            return "rate_limited"
        # Generischer API-Error
        if obj.get("isApiError"):
            return "api_overloaded"
        return None

    # Alle Zeilen parsen
    parsed_lines = []
    raw_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            parsed_lines.append(obj)
            raw_lines.append(line)
        except (ValueError, TypeError):
            continue

    if not parsed_lines:
        return empty

    # LIMIT-DETECTION: Rueckwaerts die letzten 20 Zeilen nach Fehlern scannen
    # Nicht nur die letzte Zeile! Limit kann von spaeterem tool_result ueberdeckt sein.
    is_limit = False
    limit_type = None
    reset_at = 0.0
    for i in range(len(parsed_lines) - 1, max(-1, len(parsed_lines) - 20), -1):
        obj = parsed_lines[i]
        line_text = raw_lines[i]
        err_type = _classify_error(obj, line_text)
        if err_type:
            is_limit = True
            limit_type = err_type
            reset_at = _parse_reset_epoch(line_text)
            break
        # Wenn wir eine erfolgreiche assistant-Nachricht finden, Limit aufheben
        if isinstance(obj, dict) and obj.get("type") == "assistant":
            msg = obj.get("message")
            if isinstance(msg, dict):
                sr = msg.get("stop_reason")
                if sr in ("end_turn", "stop_sequence", "tool_use"):
                    # Erfolgreiche Antwort - kein Limit
                    break

    result = {
        "internal_state": "unknown_active",
        "is_limit": is_limit,
        "limit_type": limit_type,
        "reset_at": reset_at,
        "waiting": False,
        "awaiting_approval": False,
        "last_block_type": None,
        "last_tool_name": None,
        "has_pending_tool": False,
    }

    if is_limit:
        result["internal_state"] = limit_type or "rate_limited"
        return result

    # Letzte echte user/assistant-Zeile finden (nicht interne queue-ops)
    real_last = None
    real_last_idx = -1
    for i in range(len(parsed_lines) - 1, -1, -1):
        cand = parsed_lines[i]
        if isinstance(cand, dict) and cand.get("type") in ("user", "assistant"):
            real_last = cand
            real_last_idx = i
            break

    if real_last is None:
        return result

    top_type = real_last.get("type")

    # ALLOW-DETECTION verbessert: Nur wenn tool_use OHNE folgendes tool_result
    # Schauen ob nach der letzten assistant-Zeile ein tool_result kam
    def _has_tool_result_after(assistant_idx):
        """Prueft ob nach assistant_idx ein tool_result kam."""
        for j in range(assistant_idx + 1, len(parsed_lines)):
            obj = parsed_lines[j]
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "user":
                msg = obj.get("message")
                if obj.get("toolUseResult") is not None:
                    return True
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, list):
                        for it in content:
                            if isinstance(it, dict) and it.get("type") == "tool_result":
                                return True
        return False

    if top_type == "user":
        msg = real_last.get("message") if isinstance(real_last, dict) else None
        has_tool_result = False
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for it in content:
                    if isinstance(it, dict) and it.get("type") == "tool_result":
                        has_tool_result = True
                        break
        if real_last.get("toolUseResult") is not None or has_tool_result:
            # Claude verarbeitet Tool-Ergebnis
            result["last_block_type"] = "tool_use"
            result["internal_state"] = "processing_tool_result"
            # Tool-Name aus vorheriger Assistant-Zeile
            for prev in reversed(parsed_lines[:real_last_idx]):
                if not isinstance(prev, dict) or prev.get("type") != "assistant":
                    continue
                pmsg = prev.get("message") or {}
                pc = pmsg.get("content") if isinstance(pmsg, dict) else None
                if isinstance(pc, list):
                    for it in reversed(pc):
                        if isinstance(it, dict) and it.get("type") == "tool_use":
                            result["last_tool_name"] = str(it.get("name") or "")
                            break
                break
        else:
            # User hat Prompt geschickt - Claude denkt
            result["last_block_type"] = "thinking"
            result["internal_state"] = "user_sent_prompt"

    elif top_type == "assistant":
        msg = real_last.get("message") if isinstance(real_last, dict) else None
        if isinstance(msg, dict):
            sr = msg.get("stop_reason")
            content = msg.get("content")
            has_tool_use = False
            last_tool_use_name = None

            if isinstance(content, list):
                for it in content:
                    if isinstance(it, dict) and it.get("type") == "tool_use":
                        has_tool_use = True
                        last_tool_use_name = str(it.get("name") or "")
                if content:
                    last = content[-1] if isinstance(content[-1], dict) else None
                    if last:
                        result["last_block_type"] = last.get("type")
                        if result["last_block_type"] == "tool_use":
                            result["last_tool_name"] = str(last.get("name") or "")

            if sr == "tool_use" or has_tool_use:
                has_result_after = _has_tool_result_after(real_last_idx)
                if not has_result_after:
                    result["has_pending_tool"] = True
                    if last_tool_use_name:
                        result["last_tool_name"] = last_tool_use_name
                    # "Noch kein Ergebnis" hiess bisher pauschal "wartet auf
                    # Erlaubnis". Das stimmt nur zur Haelfte - genauso gut
                    # laeuft das Werkzeug einfach noch. Waehrend eines
                    # zweiminuetigen Builds stand deshalb die ganze Zeit die
                    # Nachfrage-Animation auf dem Schirm.
                    #
                    # Ein sicheres Merkmal gibt es nicht: Claude Code schreibt
                    # die Rueckfrage ("Do you want to proceed?") nur ins
                    # Terminal, nicht ins Protokoll. Also ueber die Dauer: ein
                    # laufendes Werkzeug liefert irgendwann ein Ergebnis, eine
                    # Rueckfrage bleibt stehen bis jemand antwortet. Im Zweifel
                    # "arbeitet" - das ist der haeufigere Fall.
                    # Dreht die Laufanzeige im Fenstertitel, arbeitet Claude
                    # gerade - dann ist es keine Rueckfrage, egal wie lange es
                    # schon dauert. Ein Build ueber zwei Minuten galt vorher
                    # nach 30 Sekunden als Nachfrage.
                    #
                    # Sagt der Hook etwas dazu, gilt seine Auskunft - er weiss
                    # es, statt es abzuleiten. Sonst bleibt es beim Raten.
                    sid = os.path.splitext(os.path.basename(newest_path))[0]
                    laut_hook = _hook_wartet(sid, newest_mtime)
                    if laut_hook is None:
                        wartet = (not _claude_is_busy()
                                  and _line_age(real_last) >= APPROVAL_AFTER_S)
                    else:
                        wartet = laut_hook
                    result["awaiting_approval"] = wartet
                    result["internal_state"] = ("tool_pending_approval" if wartet
                                                else "tool_running")
                else:
                    # Ergebnis ist da - Claude verarbeitet es
                    result["internal_state"] = "tool_running"
            elif sr in ("end_turn", "stop_sequence"):
                result["waiting"] = True
                result["internal_state"] = "done"
            elif result["last_block_type"] == "thinking":
                result["internal_state"] = "thinking"
            elif result["last_block_type"] == "text":
                result["internal_state"] = "responding_text"
            else:
                result["internal_state"] = "unknown_active"

    return result


def _latest_jsonl_hits_limit(projects_dir, max_files=200, tail_kb=6):
    """Rueckwaerts-kompatibler Wrapper: nur is_limit als bool."""
    st = _latest_jsonl_status(projects_dir, max_files, tail_kb)
    return bool(st.get("is_limit"))


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return fallback


_SAVE_LOCK = threading.Lock()


def save_json(path, data):
    """Atomischer Write: erst in .tmp schreiben, dann os.replace() (atomar
    unter Windows). Verhindert dass zwei Threads gleichzeitig eine kaputte
    Datei hinterlassen."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return
    tmp = path + ".tmp"
    with _SAVE_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    # verschachtelte Defaults muessen als Kopie in `data` — sonst teilen sich
    # alle Instanzen dieselbe Referenz.
    data["buddy"] = dict(DEFAULT_SETTINGS["buddy"])
    raw = load_json(SETTINGS_FILE, None)
    if raw:
        raw_buddy = raw.get("buddy") if isinstance(raw.get("buddy"), dict) else None
        data.update(raw)
        if raw_buddy:
            merged = dict(DEFAULT_SETTINGS["buddy"])
            merged.update(raw_buddy)
            data["buddy"] = merged
        # Bestandsnutzer (Datei existiert) sehen kein Onboarding,
        # ausser der Schluessel ist bereits gesetzt.
        if "onboarded" not in raw:
            data["onboarded"] = True
    else:
        data["onboarded"] = False   # echte Erstinstallation
    # Sprache gleich hier setzen: ab jetzt uebersetzt t() richtig, auch fuer
    # alles was noch vor dem Fenster laeuft (Tray, Benachrichtigungen).
    i18n.set_lang(data.get("language", "auto"))
    return data


# --------------------------------------------------------------------------- #
#  Sessions-Ordner finden
# --------------------------------------------------------------------------- #
def detect_projects_dir():
    """Sucht den Claude-Projektordner an gaengigen Orten."""
    candidates = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        candidates.append(os.path.join(env, "projects"))
    candidates += [
        os.path.join(HOME, ".claude", "projects"),
        os.path.join(HOME, ".config", "claude", "projects"),
        os.path.join(os.environ.get("APPDATA", ""), "claude", "projects"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "claude", "projects"),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return ""


# --------------------------------------------------------------------------- #
#  Parsing der .jsonl Session-Dateien
# --------------------------------------------------------------------------- #
def _first_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
    return ""


def clean_user_text(t):
    """Entfernt System-/Befehls-Wrapper (z. B. <local-command-caveat>,
    <command-name>, <system-reminder>), damit die echte erste Frage uebrig bleibt."""
    if not t:
        return ""
    # bekannte Bloecke komplett entfernen (auch mehrzeilig)
    for tag in ("local-command-caveat", "local-command-stdout", "system-reminder",
                "command-name", "command-message", "command-args", "command-contents"):
        t = re.sub(r"<%s>.*?</%s>" % (tag, tag), " ", t, flags=re.S | re.I)
        t = re.sub(r"</?%s>" % tag, " ", t, flags=re.I)  # auch unvollstaendige
    # die typische Caveat-Warnung als Klartext entfernen, falls ohne Tags
    t = re.sub(r"Caveat:.*?unless the user explicitly asks.*?\.", " ", t, flags=re.S | re.I)
    # restliche spitzklammer-Tags raus
    t = re.sub(r"</?[a-zA-Z][\w-]*>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_session(path):
    session_id = os.path.splitext(os.path.basename(path))[0]
    ai_title = first_user = cwd = last_ts = None
    user_msgs = assistant_msgs = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue   # valides JSON, aber kein Objekt -> ueberspringen
                t = d.get("type")
                if d.get("cwd") and cwd is None:
                    cwd = d["cwd"]   # ERSTES cwd = Start-Verzeichnis (passt zum
                    #                  Projektordner; 'claude --resume' findet die
                    #                  Session nur dort, nicht in spaeteren Unterordnern)
                if d.get("timestamp"):
                    last_ts = d["timestamp"]
                if t == "ai-title":
                    ai_title = d.get("aiTitle") or ai_title
                elif t == "user":
                    user_msgs += 1
                    if first_user is None:
                        txt = clean_user_text(_first_text(d.get("message", {}).get("content")))
                        if txt:
                            first_user = txt
                elif t == "assistant":
                    assistant_msgs += 1
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if user_msgs == 0 and assistant_msgs == 0 and not ai_title:
        return None
    auto_title = ai_title or (first_user[:90] if first_user else "(ohne Titel)")
    return {
        "id": session_id,
        "auto_title": auto_title,
        "first_user": first_user or "",
        "cwd": cwd or "",
        "mtime": mtime,
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "total_msgs": user_msgs + assistant_msgs,
    }


def collect_sessions(projects_dir):
    out = []
    if not projects_dir or not os.path.isdir(projects_dir):
        return out
    for project in os.listdir(projects_dir):
        pdir = os.path.join(projects_dir, project)
        if not os.path.isdir(pdir):
            continue
        for name in os.listdir(pdir):
            if not name.endswith(".jsonl"):
                continue
            info = parse_session(os.path.join(pdir, name))
            if info:
                info["project"] = project
                out.append(info)
    return out


def fmt_time(mtime):
    d = dt.datetime.fromtimestamp(mtime)
    today = dt.date.today()
    diff = (today - d.date()).days
    if diff == 0:
        return t("heute {zeit}", zeit=d.strftime("%H:%M"))
    if diff == 1:
        return t("gestern {zeit}", zeit=d.strftime("%H:%M"))
    if diff < 7:
        # diff ist hier immer 2..6 - der Einzahlfall faellt schon auf "gestern".
        return t("vor {tage} Tagen", tage=diff)
    # Auch das Datumsformat ist uebersetzbar: im Englischen steht der Monat
    # vorn, 07.31.2026 waere dort schlicht falsch herum.
    return d.strftime(t("%d.%m.%Y"))


# --------------------------------------------------------------------------- #
#  Resume
# --------------------------------------------------------------------------- #
def decode_project(folder):
    """Rekonstruiert das Verzeichnis aus dem Projektordner-Namen.
    Achtung: verlustbehaftet (ein '-' im echten Ordnernamen ist nicht von einem
    Pfadtrenner unterscheidbar) -> nur als Notfall-Fallback verwenden."""
    if not folder:
        return ""
    return folder.replace("--", ":\\", 1).replace("-", "\\")


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_workdir(cwd, project=""):
    if cwd and os.path.isdir(cwd):
        return cwd
    dec = decode_project(project)
    return dec if dec and os.path.isdir(dec) else HOME


def _open_terminal_mac(workdir, claude_cmd, session_id, env):
    """Open macOS Terminal.app with a new window running claude --resume"""
    script = f'cd "{workdir}" && CLAUDE_CODE_FORCE_SESSION_PERSIST=1 {claude_cmd} --resume {session_id}'
    apple_script = f'''
tell application "Terminal"
    activate
    set newWindow to do shell script "{script}"
    tell application "System Events" to keystroke "n" using command down
end tell
'''
    try:
        subprocess.Popen(["osascript", "-e", apple_script], env=env)
    except Exception:
        # Fallback if AppleScript fails: try direct Terminal invocation
        try:
            subprocess.Popen(["open", "-a", "Terminal", workdir], env=env)
            # Give Terminal time to open, then send the command
            import time
            time.sleep(0.5)
            subprocess.Popen(["osascript", "-e",
                f'tell application "Terminal" to do shell script "{claude_cmd} --resume {session_id}"'],
                env=env)
        except Exception:
            pass


def resume_session(session_id, cwd, settings, project=""):
    # Session-ID hart validieren – sie fliesst in eine Terminal-Kommandozeile.
    # Ein bloedes Zeichen (& | " `) waere sonst Command-Injection.
    sid = str(session_id or "")
    if not _SESSION_ID_RE.match(sid):
        return {"ok": False, "error": t("Ungültige Session-ID.")}
    workdir = _safe_workdir(cwd, project)
    claude = settings.get("claude_cmd") or "claude"
    # `claude_cmd` kommt aus User-Settings – wir erlauben nur einen einfachen
    # Programmnamen oder absoluten Pfad, keine Shell-Metazeichen.
    if any(c in claude for c in '&|;<>"`$'):
        return {"ok": False, "error": t("Unsicherer claude_cmd-Wert.")}
    term = settings.get("terminal", "auto")
    # Wichtig: CLAUDE_CODE_FORCE_SESSION_PERSIST=1 setzen damit die resumed
    # Session weiterhin in die JSONL schreibt (sonst wird sie als Child erkannt
    # und Transcript-Speicherung ist aus).
    env = os.environ.copy()
    env["CLAUDE_CODE_FORCE_SESSION_PERSIST"] = "1"
    try:
        if _IS_MAC:
            _open_terminal_mac(workdir, claude, sid, env)
            return {"ok": True}
        elif _IS_WIN:
            if term in ("auto", "wt"):
                try:
                    # argv-Form, KEIN shell=True -> keine Shell-Interpretation
                    subprocess.Popen(["wt", "-d", workdir, "cmd", "/k",
                                      claude, "--resume", sid], env=env)
                    return {"ok": True}
                except FileNotFoundError:
                    if term == "wt":
                        return {"ok": False, "error": t("Windows Terminal (wt) nicht gefunden.")}
            # Fallback: cmd.exe direkt starten – ohne shell=True, argv als Liste.
            # `start` ist ein cmd-Builtin, deshalb rufen wir cmd /c start …
            subprocess.Popen(
                ["cmd", "/c", "start", "Claude Code", "/D", workdir,
                 "cmd", "/k", claude, "--resume", sid], env=env)
            return {"ok": True}
        else:
            return {"ok": False, "error": t("Unsupported platform")}
    except OSError as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
#  Buddy-Controller (Tkinter-Fenster in eigenem Daemon-Thread)
# --------------------------------------------------------------------------- #
_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")


def _mac_enum_monitors():
    """Get monitor information on macOS using Tkinter's screen geometry."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        monitors = []
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        root.destroy()

        monitors.append({
            "idx": 0,
            "left": 0,
            "top": 0,
            "right": screen_width,
            "bottom": screen_height,
            "primary": True,
            "label": f"{t('Primär')} · {screen_width}×{screen_height}"
        })
        return monitors
    except Exception:
        return [{
            "idx": 0,
            "left": 0,
            "top": 0,
            "right": 1920,
            "bottom": 1080,
            "primary": True,
            "label": f"{t('Primär')} · 1920×1080"
        }]


def _mac_list_windows_hwnd():
    """Get list of open windows on macOS using AppleScript."""
    try:
        script = '''tell application "System Events"
    set windowList to {}
    tell process (name of first process whose frontmost is true)
        repeat with w in (windows)
            set end of windowList to name of w
        end repeat
    end tell
    return windowList
end tell'''
        result = subprocess.check_output(["osascript", "-e", script], text=True).strip()
        windows = []
        for i, title in enumerate(result.split("\n")):
            title = title.strip()
            if title:
                windows.append((i, title))
        return windows
    except Exception:
        return []


def _win_enum_monitors():
    """Liste aller Monitore mit Arbeitsbereich, primaer-Flag, Kurzlabel.
    Rueckgabe: [{'idx': 0, 'left': ..., 'top': ..., 'right': ..., 'bottom': ...,
    'primary': True/False, 'label': 'Primär 1920×1080'}]"""
    if not _IS_WIN:
        if _IS_MAC:
            return _mac_enum_monitors()
        return []

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong),
                    ("rcMonitor", RECT),
                    ("rcWork", RECT),
                    ("dwFlags", ctypes.c_ulong)]

    u = ctypes.windll.user32
    result = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(RECT), ctypes.c_void_p)

    def cb(hmon, _hdc, _lprect, _lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            result.append({
                "left": mi.rcWork.l, "top": mi.rcWork.t,
                "right": mi.rcWork.r, "bottom": mi.rcWork.b,
                "primary": bool(mi.dwFlags & 1),
            })
        return True

    try:
        u.EnumDisplayMonitors(0, 0, MONITORENUMPROC(cb), 0)
    except Exception:
        return []

    # Primaeren nach vorne, Rest links->rechts oben->unten
    result.sort(key=lambda m: (0 if m["primary"] else 1, m["top"], m["left"]))
    for i, m in enumerate(result):
        w = m["right"] - m["left"]
        h = m["bottom"] - m["top"]
        tag = t("Primär") if m["primary"] else t("Monitor {nr}", nr=i + 1)
        m["idx"] = i
        m["label"] = f"{tag} · {w}×{h}"
    return result


def _win_monitor_work_from_point(x, y):
    """Arbeitsbereich (ohne Taskleiste) des Monitors, auf dem Punkt (x,y)
    liegt. Rueckgabe: (left, top, right, bottom) oder None."""
    if not _IS_WIN:
        if _IS_MAC:
            mons = _mac_enum_monitors()
            if mons:
                m = mons[0]
                return (m["left"], m["top"], m["right"], m["bottom"])
        return None
    try:
        u = ctypes.windll.user32

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                        ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong),
                        ("rcMonitor", RECT),
                        ("rcWork", RECT),
                        ("dwFlags", ctypes.c_ulong)]

        MonitorFromPoint = u.MonitorFromPoint
        MonitorFromPoint.restype = ctypes.c_void_p
        MonitorFromPoint.argtypes = [POINT, ctypes.c_ulong]
        hmon = MonitorFromPoint(POINT(int(x), int(y)), 2)  # NEAREST
        if not hmon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        GetMonitorInfoW = u.GetMonitorInfoW
        GetMonitorInfoW.restype = ctypes.c_bool
        GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return (mi.rcWork.l, mi.rcWork.t, mi.rcWork.r, mi.rcWork.b)
    except Exception:
        pass
    return None


def _snap_position(x, y, size_px, grid=8, edge=32):
    """Rastert (x,y) auf ein feines Raster und schnappt an Bildschirmraender.
    `size_px` ist die Kantenlaenge des Buddy-Fensters. Rueckgabe: (nx, ny)."""
    # Feines Raster (8 px) – rundet auf naechsten Rasterpunkt statt abzuschneiden
    def _snap(v, g):
        return int(round(v / g)) * g
    nx = _snap(x, grid)
    ny = _snap(y, grid)
    # Bildschirmrand-Snap (dominiert das Feinraster wenn nah dran)
    rect = _win_monitor_work_from_point(x + size_px // 2, y + size_px // 2)
    if rect:
        l, t, r, b = rect
        if abs(nx - l) < edge:
            nx = l
        elif abs((nx + size_px) - r) < edge:
            nx = r - size_px
        if abs(ny - t) < edge:
            ny = t
        elif abs((ny + size_px) - b) < edge:
            ny = b - size_px
    return int(nx), int(ny)


def _anchor_position(anchor, size_px, current_x, current_y, monitor_idx=None):
    """Springt zu einem benannten Ankerpunkt eines Monitors. anchor: tl,tc,tr,
    ml,c,mr,bl,bc,br. Wenn `monitor_idx` gesetzt ist, wird der Monitor aus
    `_win_enum_monitors()` gewaehlt; sonst der aktuelle Monitor unterm Buddy."""
    rect = None
    if monitor_idx is not None:
        mons = _win_enum_monitors()
        if 0 <= monitor_idx < len(mons):
            m = mons[monitor_idx]
            rect = (m["left"], m["top"], m["right"], m["bottom"])
    if rect is None:
        rect = _win_monitor_work_from_point(current_x + size_px // 2,
                                            current_y + size_px // 2)
    if not rect:
        return current_x, current_y
    l, t, r, b = rect
    m = 16
    xmid = (l + r - size_px) // 2
    ymid = (t + b - size_px) // 2
    pos = {
        "tl": (l + m, t + m),
        "tc": (xmid, t + m),
        "tr": (r - size_px - m, t + m),
        "ml": (l + m, ymid),
        "c":  (xmid, ymid),
        "mr": (r - size_px - m, ymid),
        "bl": (l + m, b - size_px - m),
        "bc": (xmid, b - size_px - m),
        "br": (r - size_px - m, b - size_px - m),
    }
    return pos.get(anchor, (current_x, current_y))


def _win_foreground_title():
    """Titel des aktuell fokussierten Fensters (nur Windows/macOS). Leerer String
    wenn nicht ermittelbar."""
    if _IS_MAC:
        try:
            script = 'tell application "System Events" to get name of (processes where frontmost is true)'
            result = subprocess.check_output(["osascript", "-e", script], text=True).strip()
            return result
        except Exception:
            return ""
    if not _IS_WIN:
        return ""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return ""
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def _win_process_names():
    """Liste aller aktuell laufenden Prozessnamen (lowercase). Nutzt die
    Toolhelp-Snapshot-API von Windows."""
    if not _IS_WIN:
        return []
    try:
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x2

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k = ctypes.windll.kernel32
        h = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h in (0, -1):
            return []
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        names = []
        if k.Process32First(h, ctypes.byref(pe)):
            while True:
                names.append(pe.szExeFile.decode("utf-8", errors="replace").lower())
                if not k.Process32Next(h, ctypes.byref(pe)):
                    break
        k.CloseHandle(h)
        return names
    except Exception:
        return []


def _win_process_names_with_path():
    """Wie _win_process_names, gibt aber (name, path, pid)-Tupel zurueck. Der
    Pfad kann leer sein wenn OpenProcess/QueryFullProcessImageName
    fehlschlaegt (Rechte-Problem bei System-Prozessen)."""
    if not _IS_WIN:
        return []
    try:
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x2
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k = ctypes.windll.kernel32
        h = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if h in (0, -1):
            return []
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        results = []
        if k.Process32First(h, ctypes.byref(pe)):
            while True:
                name = pe.szExeFile.decode("utf-8", errors="replace").lower()
                pid = pe.th32ProcessID
                path = ""
                # Pfad nur fuer relevante Prozesse aufloesen (spart Zeit)
                if name in ("claude.exe",):
                    try:
                        ph = k.OpenProcess(
                            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                        if ph:
                            buf = ctypes.create_unicode_buffer(1024)
                            size = wintypes.DWORD(1024)
                            if k.QueryFullProcessImageNameW(
                                    ph, 0, buf, ctypes.byref(size)):
                                path = buf.value
                            k.CloseHandle(ph)
                    except Exception:
                        path = ""
                results.append((name, path, pid))
                if not k.Process32Next(h, ctypes.byref(pe)):
                    break
        k.CloseHandle(h)
        return results
    except Exception:
        return []


# Cache fuer den Prozess-/Fenster-Scan – wird alle 2 s neu berechnet.
# "keys" ist der Fingerabdruck der aktuell offenen Claude-Kontexte (PIDs +
# Fenstertitel). Damit laesst sich unterscheiden ob nur noch dieselben
# Terminals offen sind oder ein NEUES dazugekommen ist.
_CLAUDE_CACHE = {"t": 0.0, "active": False, "keys": frozenset()}
# Genauer Titel unseres Hauptfensters (pywebview.create_window). Wichtig:
# EXAKT-Match nutzen – nicht als Substring – damit z.B. das Claude-Code-CLI
# Fenster mit Titel „⠂ Claude Session Browser Tool Development" nicht
# faelschlich als eigener Fenster aussortiert wird.
_OWN_APP_TITLE_EXACT = "claude session browser"


def _claude_context_active():
    """True wenn ein echtes Claude-CLI-Terminal offen (oder sehr kuerzlich
    aktiv) ist. Robuste Kombination:
      1) Prozess 'claude.exe' laeuft, ODER
      2) irgendein sichtbares Fenster hat 'claude' im Titel und ist nicht
         der Session Browser und nicht offensichtlich ein Browser-Tab, ODER
      3) irgendeine Session-.jsonl-Datei wurde in den letzten 5 min veraendert
         (Claude ist frisch aktiv, selbst wenn Prozess/Fenster nicht erkennbar).
    Cache 2 s."""
    now = time.time()
    if now - _CLAUDE_CACHE["t"] < 2.0:
        return _CLAUDE_CACHE["active"]
    _CLAUDE_CACHE["t"] = now
    keys = set()
    try:
        # 1) claude.exe direkt (CLI-Installation). ABSICHTLICH nicht triggern
        # fuer die Anthropic Claude Desktop-App (%LOCALAPPDATA%\AnthropicClaude\
        # claude.exe) - das ist ein Chat-Client, kein CLI, und hat nichts mit
        # unserer JSONL-Detection zu tun. Wir filtern nach Prozesspfad falls
        # verfuegbar.
        for name, path, pid in _win_process_names_with_path():
            if name != "claude.exe":
                continue
            if path and "anthropicclaude" in path.lower():
                # Desktop-App - ignorieren
                continue
            keys.add(("pid", pid))
        # 2) Fenster mit 'claude' im Titel (locker), Browser + Eigen-App raus
        # Wird auch dann durchlaufen wenn schon ein Prozess passte: die Titel
        # gehoeren mit in den Fingerabdruck, sonst faellt ein zweites Terminal
        # desselben Prozessbaums nicht auf.
        # Titel als Ausschlusskriterium taugt nur bedingt: die alte Liste
        # suchte nach " - firefox", Firefox schreibt aber "— Mozilla Firefox".
        # Ein Browser-Tab wie "3D design claudeV6 - Tinkercad" rutschte damit
        # durch und der Buddy erschien ohne jedes Terminal. Deshalb wird jetzt
        # gefragt, WELCHES PROGRAMM das Fenster besitzt - das laesst sich nicht
        # durch einen Seitentitel vortaeuschen.
        browsers = {
            "firefox.exe", "chrome.exe", "msedge.exe", "brave.exe",
            "opera.exe", "opera_gx.exe", "vivaldi.exe", "librewolf.exe",
            "zen.exe", "arc.exe", "iexplore.exe", "safari.exe",
            "thorium.exe", "chromium.exe", "waterfox.exe", "floorp.exe",
        }
        # Der Titel bleibt als zweites Netz - fuer Browser, die hier nicht
        # gelistet sind, und fuer Web-Claude in einem beliebigen Programm.
        title_hints = (
            " and 1 more page", " and 2 more page",
            "chat.openai.com", "claude.ai",   # Web-Claude nicht mitzaehlen
            "anthropic.com",
        )
        for hwnd, title in _win_list_windows_hwnd():
            t = title.lower()
            if not t or t.strip() == _OWN_APP_TITLE_EXACT:
                continue
            if "claude" not in t:
                continue
            if _win_hwnd_process(hwnd) in browsers:
                continue
            if any(b in t for b in title_hints):
                continue
            # Fenster-Handle statt Titel: der Titel eines Claude-Terminals
            # aendert sich staendig (Spinner-Zeichen, aktueller Task) - als
            # Fingerabdruck waere er wertlos, das Handle bleibt stabil.
            keys.add(("win", hwnd))
        # KEIN mtime-Fallback mehr: Buddy soll direkt verschwinden wenn die
        # Claude-CLI geschlossen wird – nicht noch 5 Min nach Aktivitaet
        # sichtbar bleiben. Erkennung nur ueber laufenden Prozess + offenes
        # Fenster.
    except Exception:
        pass
    _CLAUDE_CACHE["keys"] = frozenset(keys)
    _CLAUDE_CACHE["active"] = bool(keys)
    return _CLAUDE_CACHE["active"]


def _snooze_over(snooze, keys, empty_since, now, grace=5.0):
    """Ist der Buddy-Snooze vorbei? -> (vorbei, neues_empty_since)

    `snooze` ist der Kontext-Fingerabdruck vom Moment des Wegklickens.
    Vorbei ist der Snooze wenn ein Kontext dazugekommen ist (neues Terminal),
    oder wenn alle damals offenen Kontexte seit `grace` Sekunden weg sind.
    Die Karenzzeit faengt ab, dass ein Fenstertitel mal kurz kein "claude"
    enthaelt - das soll den Buddy nicht zurueckholen.
    """
    if keys - snooze:
        return True, 0.0
    if keys or not snooze:
        # Unveraenderte Lage, oder von Anfang an kein Kontext bekannt: dann
        # beendet nur ein neuer Kontext den Snooze.
        return False, 0.0
    if not empty_since:
        return False, now
    return (now - empty_since >= grace), empty_since


def _claude_context_keys():
    """Fingerabdruck der gerade offenen Claude-Kontexte (siehe _CLAUDE_CACHE).

    Nutzt denselben 2-s-Cache wie _claude_context_active - der Aufruf kostet
    also nichts extra wenn beides im selben Tick abgefragt wird.
    """
    _claude_context_active()
    return _CLAUDE_CACHE["keys"]


_HWND_PROC_CACHE = {}


def _win_hwnd_process(hwnd):
    """Dateiname des Programms, dem ein Fenster gehoert (klein, z.B.
    'firefox.exe'). Leer wenn nicht ermittelbar.

    Zwischengespeichert: das Handle bleibt fuer die Lebensdauer des Fensters
    gleich, der Prozess dahinter auch - und die Abfrage kostet zwei
    Systemaufrufe pro Fenster, das waere im 2-Sekunden-Takt Verschwendung.
    """
    if not _IS_WIN:
        return ""
    hit = _HWND_PROC_CACHE.get(hwnd)
    if hit is not None:
        return hit
    name = ""
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # PROCESS_QUERY_LIMITED_INFORMATION - reicht fuer den Pfad und geht
        # auch bei Prozessen anderer Rechte-Stufe.
        h = k.OpenProcess(0x1000, False, pid.value)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(520)
                size = ctypes.c_ulong(520)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
            finally:
                k.CloseHandle(h)
    except Exception:
        name = ""
    if len(_HWND_PROC_CACHE) > 400:
        _HWND_PROC_CACHE.clear()      # geschlossene Fenster nicht ewig halten
    _HWND_PROC_CACHE[hwnd] = name
    return name


def _win_list_windows_hwnd():
    """Sichtbare Fenster als (hwnd, titel)-Liste, ungefiltert."""
    if _IS_MAC:
        return _mac_list_windows_hwnd()
    if not _IS_WIN:
        return []
    try:
        u = ctypes.windll.user32
        out = []

        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                      ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _lparam):
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            t = (buf.value or "").strip()
            if t and len(t) < 200:
                out.append((int(hwnd or 0), t))
            return True

        u.EnumWindows(EnumProc(cb), 0)
        return out
    except Exception:
        return []


def _win_list_windows():
    """Liste sichtbarer Fenstertitel (Duplikate raus). Fuer den Picker im
    Buddy-Tab."""
    seen = []
    seen_set = set()
    for _hwnd, t in _win_list_windows_hwnd():
        if t not in seen_set:
            seen_set.add(t)
            seen.append(t)
    return sorted(seen, key=str.lower)


def _latest_session_mtime(projects_dir, max_files=200):
    """Neueste mtime aller .jsonl-Dateien unter projects_dir. 0 wenn nichts
    gefunden. Bricht nach `max_files` ab um die Latenz klein zu halten."""
    if not projects_dir or not os.path.isdir(projects_dir):
        return 0.0
    latest = 0.0
    count = 0
    try:
        for entry in os.scandir(projects_dir):
            if not entry.is_dir():
                continue
            try:
                for sub in os.scandir(entry.path):
                    if sub.is_file() and sub.name.endswith(".jsonl"):
                        try:
                            m = sub.stat().st_mtime
                            if m > latest:
                                latest = m
                        except OSError:
                            pass
                        count += 1
                        if count >= max_files:
                            return latest
            except OSError:
                pass
    except OSError:
        pass
    return latest


def _draw_frame_on_canvas(canvas, style, w, h, pad, color, label, chroma):
    """Zeichnet Rahmen-Layer auf einem tk.Canvas und gibt die IDs zurueck.
    Zerlegt in eine Funktion pro Design."""
    if style == "off":
        return []
    if style in ("classic", "webcam"):
        return _draw_frame_classic(canvas, w, h, pad, color, label)
    if style == "neon":
        return _draw_frame_neon(canvas, w, h, pad, color, label)
    if style == "panel":
        return _draw_frame_panel(canvas, w, h, pad, color, label)
    return []


def _draw_frame_classic(canvas, w, h, pad, color, label):
    """Tech-Rahmen mit achteckigen Ecken, Nameplate unten, LIVE-Dot."""
    ids = []
    cut = max(4, pad["l"] // 2)
    dark = "#14100e"
    darker = _shade_hex(color, 0.55)
    accent_dim = _shade_hex(color, 0.75)
    cream = "#F1EBDD"

    outer = [cut, 0, w - cut, 0, w, cut, w, h - cut,
             w - cut, h, cut, h, 0, h - cut, 0, cut]
    ids.append(canvas.create_polygon(outer, fill=color, outline=""))

    cam_l = pad["l"]; cam_t = pad["t"]
    cam_r = w - pad["r"]; cam_b = h - pad["b"] + 2
    ids.append(canvas.create_rectangle(cam_l, cam_t, cam_r, cam_b,
                                       fill=dark, outline=""))

    corner_len = max(4, pad["l"] // 2)
    for cx, cy, dx, dy in (
        (cam_l, cam_t,  1,  1), (cam_r, cam_t, -1,  1),
        (cam_l, cam_b,  1, -1), (cam_r, cam_b, -1, -1),
    ):
        ids.append(canvas.create_line(cx, cy, cx + dx * corner_len, cy,
                                      fill=color, width=2))
        ids.append(canvas.create_line(cx, cy, cx, cy + dy * corner_len,
                                      fill=color, width=2))

    stripe_w = max(6, w // 8)
    top_y = pad["t"] // 2
    for dx in (-stripe_w - 4, 0, stripe_w + 4):
        cx = w // 2 + dx
        ids.append(canvas.create_line(cx - 3, top_y, cx + 3, top_y,
                                      fill=darker, width=2))

    plate_top = h - pad["b"] + 3
    plate_bot = h - 3
    plate_half = min(w // 2 - 6, max(24, int(w * 0.36)))
    plate_cx = w // 2
    trap = [plate_cx - plate_half + 6, plate_top,
            plate_cx + plate_half - 6, plate_top,
            plate_cx + plate_half, plate_bot,
            plate_cx - plate_half, plate_bot]
    ids.append(canvas.create_polygon(trap, fill=darker, outline=""))
    ids.append(canvas.create_line(
        plate_cx - plate_half + 8, plate_top + 1,
        plate_cx + plate_half - 8, plate_top + 1,
        fill=accent_dim, width=1))
    font_size = max(6, min(11, (plate_bot - plate_top) - 4))
    ids.append(canvas.create_text(
        plate_cx, (plate_top + plate_bot) // 2,
        text=(label or "CLAWD").upper()[:7],
        fill=cream, font=("Segoe UI", font_size, "bold")))

    dot_r = max(2, pad["t"] // 3)
    dot_cx = w - pad["r"] - dot_r - 2
    dot_cy = pad["t"] // 2
    ids.append(canvas.create_oval(
        dot_cx - dot_r, dot_cy - dot_r,
        dot_cx + dot_r, dot_cy + dot_r,
        fill="#ff3a5a", outline=""))
    return ids


def _draw_frame_neon(canvas, w, h, pad, color, label):
    """Doppelte leuchtende Kontur, transparent innen, kein Nameplate.
    Ausserer Ring in dunklerem Ton, innerer in Vollton – wirkt wie Glow."""
    ids = []
    dim = _shade_hex(color, 0.45)
    # Aeussere weite duenne Linie
    ids.append(canvas.create_rectangle(0, 0, w - 1, h - 1,
                                       outline=dim, width=2))
    # Innere dickere leuchtende Linie
    inset = max(2, pad["l"] // 2)
    ids.append(canvas.create_rectangle(inset, inset, w - 1 - inset, h - 1 - inset,
                                       outline=color, width=2))
    # Kleine Ecken-Akzente (Diagonal-Striche)
    corner = max(3, pad["l"] // 2)
    for cx, cy, dx, dy in (
        (0, 0, 1, 1), (w - 1, 0, -1, 1),
        (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1),
    ):
        ids.append(canvas.create_line(
            cx, cy, cx + dx * corner, cy + dy * corner,
            fill=color, width=2))
    return ids


def _draw_frame_panel(canvas, w, h, pad, color, label):
    """Flache Titelleiste oben mit Live-Dot + Text, dunkle Cam-Flaeche,
    schmaler Akzentrand unten."""
    ids = []
    dark = "#14100e"
    darker = _shade_hex(color, 0.35)
    cream = "#F1EBDD"

    # Hauptrahmen als voll gefuelltes Rechteck
    ids.append(canvas.create_rectangle(0, 0, w - 1, h - 1,
                                       fill=darker, outline=""))
    # Titelleiste oben in Akzentfarbe
    title_h = pad["t"]
    ids.append(canvas.create_rectangle(0, 0, w, title_h,
                                       fill=color, outline=""))
    # Cam-Flaeche
    ids.append(canvas.create_rectangle(pad["l"], pad["t"],
                                       w - pad["r"], h - pad["b"],
                                       fill=dark, outline=""))
    # LIVE-Dot ganz links in der Titelleiste
    dot_r = max(2, title_h // 4)
    dot_cx = pad["l"] + dot_r + 2
    dot_cy = title_h // 2
    ids.append(canvas.create_oval(
        dot_cx - dot_r, dot_cy - dot_r,
        dot_cx + dot_r, dot_cy + dot_r,
        fill="#ff3a5a", outline=""))
    # Titel-Text daneben
    font_size = max(6, min(10, title_h - 4))
    ids.append(canvas.create_text(
        dot_cx + dot_r + 5, dot_cy,
        anchor="w",
        text=(label or "CLAWD").upper()[:10],
        fill=cream, font=("Segoe UI", font_size, "bold")))
    return ids


def _hintergrund_index(frame):
    """Welcher Paletten-Index ist die Hintergrundflaeche des Sprites?

    Nicht ueber die Farbe entscheiden: in „idle breathe" ist der Hintergrund
    #000000 und die Augen sind #080C08 - beides praktisch schwarz. Wer nach
    Farbe geht, radiert die Augen mit aus. Auch ein fester Index taugt nicht,
    er ist je Animation ein anderer (1 bei den meisten, 9 bei „work coding").

    Deshalb die Mehrheit am Bildrand: das Motiv sitzt in der Mitte, der Rand
    gehoert dem Hintergrund. Das Eckpixel allein reicht nicht - in zwei
    Bildern von „limit" schlaegt der rote Blitz bis in eine Ecke.
    """
    zaehler = {}
    rand = ([frame[c] for c in range(20)]
            + [frame[19 * 20 + c] for c in range(20)]
            + [frame[r * 20] for r in range(1, 19)]
            + [frame[r * 20 + 19] for r in range(1, 19)])
    for v in rand:
        zaehler[v] = zaehler.get(v, 0) + 1
    return max(zaehler.items(), key=lambda kv: kv[1])[0]


def _resolved_frame_style(bud):
    """Frame-Style aus Config lesen. Migration: webcam/neon/panel -> classic."""
    st = bud.get("frame_style")
    if st in ("webcam", "neon", "panel"):
        return "classic"
    if st in ("off", "classic"):
        return st
    return "off"


def _frame_pad(style, scale):
    """Padding pro Kante fuer die verschiedenen Rahmen-Styles."""
    if style == "classic":
        b = max(9, scale * 3)
        return {"l": b, "r": b, "t": b, "b": b + max(12, scale * 3), "style": "classic"}
    if style == "neon":
        b = max(6, scale * 2)
        return {"l": b, "r": b, "t": b, "b": b, "style": "neon"}
    if style == "panel":
        b = max(6, scale * 2)
        return {"l": b, "r": b, "t": b + max(12, scale * 3), "b": b, "style": "panel"}
    return {"l": 0, "r": 0, "t": 0, "b": 0, "style": "off"}


def _shade_hex(hex_color, factor):
    """Multipliziert alle RGB-Kanaele mit `factor`. Fuer dunklere Toene."""
    try:
        c = hex_color.lstrip("#")
        r = min(255, max(0, int(int(c[0:2], 16) * factor)))
        g = min(255, max(0, int(int(c[2:4], 16) * factor)))
        b = min(255, max(0, int(int(c[4:6], 16) * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


class BuddyController:
    """Zeigt einen kleinen Clawd-Buddy als frameloses, transparentes,
    always-on-top Tkinter-Fenster. Laeuft in einem Daemon-Thread. Wechselt
    die Animation abhaengig von der Aktivitaet in ~/.claude/projects/*."""

    _TRANSPARENT = "magenta"        # Chroma-Key (unwahrscheinlich in Sprites)
    _TICK_MS = 100                  # State-Polling/Visibility/Fade/Hover alle 100ms
    _FRAME_MS = 180                 # Frame-Advance nur alle 180ms (~5.5 fps)
    _STATE_DEBOUNCE_S = 1.5         # min. Standzeit bevor Buddy in andere Anim wechselt
    _POLL_MS = 300                  # Zustands-/Fokus-Check-Rate
    _MTIME_CACHE_S = 2.0            # nur alle 2s Dateisystem abfragen
    _FG_CHECK_EVERY = 3             # foreground-title nur alle N ticks (~360 ms)

    def __init__(self, api):
        self.api = api              # -> hat .settings und ._projects_dir()
        self._thread = None
        self._alive = False
        self._q = queue.Queue()     # Commands aus dem UI-Thread
        self._pulse = 0             # fuer die kurzen "surprise"-Momente
        # Name der gerade laufenden Animation, damit andere Threads (z.B. der
        # Clawdmeter-Link) sie mitlesen koennen ohne in den Tk-Thread zu
        # greifen. Ein einzelner String-Zuweisung braucht kein Lock.
        self._pub_anim = ""
        # Limit-Lage zum Mitlesen (Einstellungs-Seite). Ein Tupel wird in
        # einem Rutsch ersetzt, nie halb beschrieben - deshalb kein Lock.
        self._pub_limit = (False, 0.0)   # (im Limit?, bis wann)
        self._pub_rect = None            # (x, y, w, h) solange sichtbar

    def _app_window_visible(self):
        """True wenn das Session-Browser-Hauptfenster gerade wirklich als
        sichtbares Fenster existiert (nicht im Tray minimiert/verstecked)."""
        if not _IS_WIN:
            return True
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            found = {"ok": False}
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def cb(hwnd, _lp):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.strip().lower() == _OWN_APP_TITLE_EXACT:
                    found["ok"] = True
                    return False
                return True

            user32.EnumWindows(EnumWindowsProc(cb), 0)
            return found["ok"]
        except Exception:
            return True

    # ---- oeffentliche API (aus Api heraus aufgerufen) ----
    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def current_anim(self):
        """Name der Animation die der Buddy gerade zeigt, sonst "".

        Laeuft der Buddy nicht (in den Einstellungen aus), gibt es nichts zu
        spiegeln -- dann liefert das hier "" und das Clawdmeter waehlt wieder
        selbst nach Auslastung aus."""
        if not self.is_alive():
            return ""
        return self._pub_anim or ""

    def start(self):
        if self.is_alive():
            return
        if not BUDDY_ANIMS:
            return                  # Sprites konnten nicht dekodiert werden
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="BuddyThread")
        self._thread.start()

    def stop(self):
        if not self.is_alive():
            return
        self._q.put(("quit", None))

    def push(self, key=None):
        """Buddy weiss von aussen dass sich was geaendert hat (Groesse,
        Sichtbarkeit, ...). Bei disabled -> stop, bei enabled+aus -> start."""
        s = self.api.settings.get("buddy", {})
        if not s.get("enabled"):
            self.stop()
            return
        if not self.is_alive():
            self.start()
            return
        self._q.put(("refresh", key))

    def surprise(self):
        """Kurze 'surprise'-Animation ausloesen (z.B. Test-Button)."""
        if self.is_alive():
            self._q.put(("pulse", "surprise"))

    def _notify_limit_reset(self):
        """Feuert die Limit-Reset-Karte (persistent, dismissible) + optional
        Windows-Tray-Notification. Doppel-Feuer wird via
        limit_reset_notified_for verhindert."""
        if not self.api.settings.get("notify_limit_reset", True):
            return
        # Doppel-Schutz: fuer welche Reset-Zeit haben wir schon benachrichtigt?
        reset_at = float(self.api.settings.get("limit_reset_at", 0) or 0)
        notified_for = float(
            self.api.settings.get("limit_reset_notified_for", 0) or 0)
        if reset_at > 0 and abs(notified_for - reset_at) < 30:
            return
        # Karte zeigen
        try:
            toast = getattr(self.api, "_reset_toast", None)
            if toast is None:
                toast = LimitResetToast()
                self.api._reset_toast = toast
            toast.show(avoid=self._pub_rect)
        except Exception:
            pass
        # Zusaetzlich Tray-Notification als Bonus
        tray = getattr(self.api, "_tray", None)
        if tray and tray.icon:
            try:
                tray.icon.notify(
                    t("Dein Claude-Limit ist zurück – weitermachen!"),
                    "Clawd")
            except Exception:
                pass
        # Buddy kurz "surprise" spielen wenn er sichtbar ist
        try:
            if self.is_alive():
                self._q.put(("pulse", "surprise"))
        except Exception:
            pass
        # Marker speichern damit's nicht doppelt feuert
        try:
            if reset_at > 0:
                self.api.settings["limit_reset_notified_for"] = reset_at
            self.api.settings["limit_reset_at"] = 0
            save_json(SETTINGS_FILE, self.api.settings)
        except Exception:
            pass

    def _schedule_reset_timer(self, reset_at):
        """Startet einen Timer-Thread der genau zur Reset-Zeit die Karte
        feuert – unabhaengig davon ob gerade JSONL-Aktivitaet gepollt wird."""
        try:
            delay = max(0.0, float(reset_at) - time.time())
        except (TypeError, ValueError):
            return
        # Cap auf 26h damit ein kaputter Parse nicht ewig einen Timer blockiert
        if delay > 26 * 3600:
            return
        prev = getattr(self, "_reset_timer", None)
        if prev is not None:
            try:
                prev.cancel()
            except Exception:
                pass
        try:
            t = threading.Timer(delay + 1.0, self._notify_limit_reset)
            t.daemon = True
            t.start()
            self._reset_timer = t
        except Exception:
            pass

    def preview_anim(self, name, seconds=3.0):
        """Zeigt eine bestimmte Animation fuer `seconds` Sekunden – ueberschreibt
        die Auto-Erkennung waehrenddessen."""
        if self.is_alive():
            self._q.put(("preview", (name, float(seconds))))

    def jump_to(self, x, y):
        """Buddy exakt auf (x,y) setzen."""
        if self.is_alive():
            self._q.put(("jump", (int(x), int(y))))

    def place_mode(self, on_done=None):
        """Positionier-Modus: Buddy pulsiert damit man ihn leicht findet,
        und der `on_done`-Callback wird nach dem ersten Drop aufgerufen
        (typisch: Hauptfenster wiederherstellen)."""
        self._on_place_done = on_done
        if self.is_alive():
            self._q.put(("place", True))

    # ---- interner Thread ----
    def _run(self):
        try:
            import tkinter as tk
        except Exception:
            self._alive = False
            return

        s = dict(self.api.settings.get("buddy", {}))
        scale = max(2, min(10, int(s.get("size", 4))))
        opacity = max(20, min(100, int(s.get("opacity", 100)))) / 100.0
        x, y = int(s.get("x", 200)), int(s.get("y", 200))

        root = tk.Tk()
        root.withdraw()  # spaeter deiconify, damit initial kein Flackern
        try:
            root.title("Clawd")
        except Exception:
            pass
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", self._TRANSPARENT)
        except Exception:
            pass
        try:
            root.attributes("-alpha", 0.0)
        except Exception:
            pass
        root.configure(bg=self._TRANSPARENT)

        # Rahmen einlesen und Fenstermaße daraus ableiten. Sprite bleibt
        # immer 20*scale, das Fenster wird um Padding groesser.
        frame_style = _resolved_frame_style(s)
        frame_pad = _frame_pad(frame_style, scale)
        px_sprite = 20 * scale
        px_w = px_sprite + frame_pad["l"] + frame_pad["r"]
        px_h = px_sprite + frame_pad["t"] + frame_pad["b"]
        root.geometry(f"{px_w}x{px_h}+{x}+{y}")

        canvas = tk.Canvas(root, width=px_w, height=px_h,
                           bg=self._TRANSPARENT,
                           highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        # Rahmen (Layer 0) und Sprite-Image (Layer 1) auf Canvas.
        frame_items = _draw_frame_on_canvas(
            canvas, frame_style, px_w, px_h, frame_pad,
            s.get("frame_color") or "#ec7456",
            s.get("frame_label") or "CLAWD",
            self._TRANSPARENT)

        # PhotoImage als Zeichenflaeche – Sprite zentriert im Innenbereich
        img = tk.PhotoImage(width=px_sprite, height=px_sprite)
        sprite_id = canvas.create_image(frame_pad["l"], frame_pad["t"],
                                        anchor="nw", image=img)
        canvas.image = img
        # Positionier-Highlight (unsichtbar bis place_mode)
        place_ring = canvas.create_rectangle(
            1, 1, px_w - 1, px_h - 1,
            outline="#ffd66b", width=3, state="hidden")

        def rebuild_frame(new_style, new_color, new_label, new_scale):
            """Loescht alle Frame-Layer und zeichnet sie neu; passt Fenster-,
            Canvas- und Bildgroesse an."""
            nonlocal frame_style, frame_pad, px_sprite, px_w, px_h, frame_items
            # Sprite-Zeichenflaeche und Fenster neu dimensionieren
            frame_style = new_style
            frame_pad = _frame_pad(new_style, new_scale)
            px_sprite = 20 * new_scale
            px_w = px_sprite + frame_pad["l"] + frame_pad["r"]
            px_h = px_sprite + frame_pad["t"] + frame_pad["b"]
            try:
                img.configure(width=px_sprite, height=px_sprite)
                canvas.configure(width=px_w, height=px_h)
                root.geometry(f"{px_w}x{px_h}")
                canvas.coords(sprite_id, frame_pad["l"], frame_pad["t"])
                canvas.coords(place_ring, 1, 1, px_w - 1, px_h - 1)
            except Exception:
                pass
            # Alte Frame-Layer weg, neue drauf
            for fid in frame_items:
                try: canvas.delete(fid)
                except Exception: pass
            frame_items = _draw_frame_on_canvas(
                canvas, frame_style, px_w, px_h, frame_pad,
                new_color, new_label, self._TRANSPARENT)
            # Sprite und place_ring wieder in den Vordergrund heben
            try:
                canvas.tag_raise(sprite_id)
                canvas.tag_raise(place_ring)
            except Exception:
                pass
            state["px_w"] = px_w
            state["px_h"] = px_h
            state["frame_style"] = frame_style
            state["frame_color"] = new_color
            state["frame_label"] = new_label
            # Render-Cache wegwerfen – neuer BG oder Groesse.
            render_cache.clear()
            last_drawn["key"] = None

        state = {
            "scale": scale,
            "opacity": opacity,
            "anim": "idle breathe",
            "frame": 0,
            "frame_style": frame_style,
            "frame_color": s.get("frame_color") or "#ec7456",
            "frame_label": s.get("frame_label") or "CLAWD",
            "px_w": px_w,
            "px_h": px_h,
            "live_pulse": 0.0,
            "last_mtime_check": 0.0,
            "last_mtime": 0.0,
            "activity_state": "idle",
            "surprise_until": 0.0,
            "placing": False,          # Position-Modus: dickes Highlight-Rechteck
            "place_pulse": 0.0,
            "preview_until": 0.0,
            "preview_anim": "",
            "overlay": None,           # Vollflaechen-Toplevel im Platzier-Modus
            "current_alpha": 0.0,      # tatsaechliche Fenster-Deckkraft (fuer Fade)
            "target_alpha": 0.0,
            "was_visible": False,      # letzter apply_visibility-Zustand
            "tick": 0,
            "hover": False,            # Maus ueber Buddy -> transparent machen
            # None = kein Snooze. Sonst: Fingerabdruck der Claude-Kontexte die
            # beim Wegklicken offen waren (siehe desired_visible).
            "snooze_keys": None,
            "snooze_empty_since": 0.0,
        }

        # ---- Drag & Drop ----
        drag = {"x": 0, "y": 0, "moved": False}

        def on_press(e):
            drag["x"] = e.x
            drag["y"] = e.y
            drag["moved"] = False

        def on_drag(e):
            nx = root.winfo_x() + e.x - drag["x"]
            ny = root.winfo_y() + e.y - drag["y"]
            # Raster + Bildschirmrand-Snap
            size_px = state.get("px_w", 20 * state["scale"])
            nx, ny = _snap_position(nx, ny, size_px)
            root.geometry(f"+{nx}+{ny}")
            drag["moved"] = True

        def on_release(e):
            if drag["moved"]:
                nx, ny = root.winfo_x(), root.winfo_y()
                bud = self.api.settings.setdefault("buddy", {})
                bud["x"], bud["y"] = nx, ny
                try:
                    save_json(SETTINGS_FILE, self.api.settings)
                except Exception:
                    pass
            if state["placing"] and drag["moved"]:
                end_place_mode()

        canvas.bind("<Button-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        # Doppelklick oder Rechtsklick -> voruebergehend wegschicken (Snooze)
        canvas.bind("<Double-Button-1>",
                    lambda e: self._q.put(("hide_toggle", None)))
        canvas.bind("<Button-3>",
                    lambda e: self._q.put(("hide_toggle", None)))
        # Maus rein/raus -> sofort transparent damit man drunter sieht
        def _on_enter(e):
            state["hover"] = True
            state["hover_started_at"] = time.time()
            # Sofort auf 15% Deckkraft (kein Fade)
            try:
                target = min(state["opacity"], 0.15)
                state["current_alpha"] = target
                state["target_alpha"] = target
                root.attributes("-alpha", target)
            except Exception:
                pass
        def _on_leave(e):
            state["hover"] = False
            state["hover_started_at"] = 0.0
            state["wink_fired_this_hover"] = False
            # Sofort zurueck auf normale Deckkraft (kein Fade)
            try:
                if state.get("was_visible"):
                    op = state["opacity"]
                    state["current_alpha"] = op
                    state["target_alpha"] = op
                    root.attributes("-alpha", op)
            except Exception:
                pass
        canvas.bind("<Enter>", _on_enter)
        canvas.bind("<Leave>", _on_leave)

        # ---- Overlay fuer Platzier-Modus ----
        def virtual_desktop_bounds():
            if _IS_WIN:
                try:
                    u = ctypes.windll.user32
                    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                            u.GetSystemMetrics(78), u.GetSystemMetrics(79))
                except Exception:
                    pass
            return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())

        def end_place_mode(save_pos=True):
            if not state["placing"]:
                return
            state["placing"] = False
            try:
                canvas.itemconfigure(place_ring, state="hidden")
            except Exception:
                pass
            ov = state.get("overlay")
            if ov is not None:
                try:
                    ov.destroy()
                except Exception:
                    pass
                state["overlay"] = None
            cb = getattr(self, "_on_place_done", None)
            if cb:
                try:
                    cb()
                except Exception:
                    pass

        def build_overlay():
            try:
                vx, vy, vw, vh = virtual_desktop_bounds()
                ov = tk.Toplevel(root)
                ov.overrideredirect(True)
                ov.attributes("-topmost", True)
                ov.attributes("-alpha", 0.42)
                ov.configure(bg="#0a0b0d")
                ov.geometry(f"{vw}x{vh}+{vx}+{vy}")
                cv = tk.Canvas(ov, bg="#0a0b0d",
                               highlightthickness=0, bd=0)
                cv.pack(fill="both", expand=True)
                # Feinraster
                for xi in range(0, vw, 20):
                    cv.create_line(xi, 0, xi, vh, fill="#3a3d42")
                for yi in range(0, vh, 20):
                    cv.create_line(0, yi, vw, yi, fill="#3a3d42")
                # 100er-Raster kraeftiger
                for xi in range(0, vw, 100):
                    cv.create_line(xi, 0, xi, vh, fill="#5c6068", width=1)
                for yi in range(0, vh, 100):
                    cv.create_line(0, yi, vw, yi, fill="#5c6068", width=1)
                # Nur ESC bricht ab – ein Klick soll den Buddy greifen koennen,
                # nicht das Overlay treffen.
                ov.bind("<Escape>",
                        lambda e: end_place_mode(save_pos=False))
                cv.bind("<Escape>",
                        lambda e: end_place_mode(save_pos=False))
                # Das Raster darf die Maus nie sehen. Sonst faengt es den Griff
                # nach dem Buddy ab und Platzieren tut schlicht nichts.
                # Tastatur (ESC) kommt trotzdem an - WS_EX_TRANSPARENT
                # betrifft nur den Maus-Treffertest.
                if _IS_WIN:
                    try:
                        ov.update_idletasks()
                        u = ctypes.windll.user32
                        hwnd = ov.winfo_id()
                        parent = u.GetParent(hwnd)
                        if parent:
                            hwnd = parent
                        GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x00000020
                        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
                        u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                         ex | WS_EX_TRANSPARENT)
                    except Exception:
                        pass

                # Beide Fenster sind topmost, vorn liegt der zuletzt
                # angehobene. focus_force() holt das Overlay nach vorn, also
                # muss der Buddy DANACH angehoben werden - nicht davor.
                try:
                    ov.update_idletasks()
                    cv.focus_set()
                    ov.focus_force()      # ESC-Fokus; hebt das Overlay an
                    root.attributes("-topmost", False)
                    root.attributes("-topmost", True)
                    root.lift()           # ... und der Buddy wieder darueber
                except Exception:
                    pass
                return ov
            except Exception:
                return None

        # ---- Sichtbarkeits-Logik (mit throttled foreground-check) ----
        fg_cache = {"title": "", "tick": -999}

        def _fg_title(tick_count):
            if tick_count - fg_cache["tick"] >= self._FG_CHECK_EVERY:
                fg_cache["title"] = _win_foreground_title().lower()
                fg_cache["tick"] = tick_count
            return fg_cache["title"]

        def _desired_visible_raw():
            bud = self.api.settings.get("buddy", {})
            if not bud.get("enabled"):
                return False
            # Buddy-Tab: immer sichtbar (auch waehrend Foreground-Racing) -
            # ABER nur wenn das Hauptfenster auch tatsaechlich sichtbar ist.
            # Wenn die App in den Tray minimiert wurde bleibt _current_view
            # zwar auf "buddy" stehen, aber dann sollen die normalen
            # Sichtbarkeits-Regeln (when_claude etc.) greifen statt Buddy
            # ewig auf dem Desktop rumhaengen zu lassen.
            if getattr(self.api, "_current_view", "") == "buddy":
                if self._app_window_visible():
                    return True
            fg = _fg_title(state.get("tick", 0))
            # Session Browser vorne (auf anderem Tab) -> Buddy weg.
            if fg.strip() == _OWN_APP_TITLE_EXACT:
                return False
            mode = bud.get("visibility", "when_claude")
            if mode == "always":
                return True
            if mode == "when_window":
                needle = (bud.get("target_window") or "").lower().strip()
                if not needle:
                    return True
                return needle in fg
            if mode == "when_claude":
                return _claude_context_active()
            return True

        def desired_visible():
            """Wie _desired_visible_raw, aber mit Snooze.

            Doppel-/Rechtsklick blendet den Buddy nur voruebergehend aus - er
            bleibt in den Settings aktiviert. Zurueck kommt er sobald ein
            NEUER Claude-Kontext auftaucht (neues Terminal) oder alle
            weggeklickten Terminals zu sind.

            Bewusst NICHT beendet wird der Snooze durch blosses Alt-Tabben:
            dass der Buddy nach den normalen Regeln gerade unsichtbar ist
            heisst nicht, dass der User ihn wiederhaben will.
            """
            want = _desired_visible_raw()
            snooze = state.get("snooze_keys")
            if snooze is not None:
                keys = _claude_context_keys()
                on_buddy_tab = (
                    getattr(self.api, "_current_view", "") == "buddy"
                    and self._app_window_visible())
                if state.get("placing") or on_buddy_tab:
                    # Buddy-Tab offen oder Platzier-Modus -> immer zeigen,
                    # sonst sucht der User im Leeren.
                    state["snooze_keys"] = None
                    state["snooze_empty_since"] = 0.0
                else:
                    over, since = _snooze_over(
                        snooze, keys,
                        state.get("snooze_empty_since") or 0.0, time.time())
                    state["snooze_empty_since"] = since
                    if not over:
                        return False
                    state["snooze_keys"] = None
            return want

        _visible = {"v": None}
        state["invisible_since"] = 0.0  # Zeit seit want=False (0 wenn sichtbar)

        def apply_visibility():
            want = desired_visible()
            # Fade-Ziel aktualisieren – nicht sofort withdraw/deiconify.
            # Bei Maus-Hover deutlich transparenter (max 20% der eingest. Opacity).
            if want:
                if state.get("hover"):
                    state["target_alpha"] = min(state["opacity"], 0.15)
                else:
                    state["target_alpha"] = state["opacity"]
                state["invisible_since"] = 0.0
            else:
                state["target_alpha"] = 0.0
                # Merken seit wann "unsichtbar gewollt" - fuer Selbst-Heiler
                if state["invisible_since"] == 0.0:
                    state["invisible_since"] = time.time()
            if want and not state["was_visible"]:
                # Reingekommen -> Fenster zeigen (transparent) und Fade starten
                try:
                    root.attributes("-alpha", 0.0)
                    state["current_alpha"] = 0.0
                    root.deiconify()
                except Exception:
                    pass
            state["was_visible"] = want
            _visible["v"] = want
            # Selbst-Heiler: wenn wir seit >4s unsichtbar sein sollen aber
            # das Fenster noch da rumhaengt (Fade-Loop gecrasht, tk-Bug, was
            # auch immer), hart wegschicken.
            if (not want) and state["invisible_since"] > 0 \
                    and (time.time() - state["invisible_since"]) > 4.0:
                try:
                    root.attributes("-alpha", 0.0)
                    state["current_alpha"] = 0.0
                    root.withdraw()
                except Exception:
                    pass

        def step_fade():
            # Naehert current_alpha an target_alpha an. ~180 ms Total.
            cur = state["current_alpha"]
            tgt = state["target_alpha"]
            if abs(cur - tgt) < 0.02:
                if cur != tgt:
                    state["current_alpha"] = tgt
                    try:
                        root.attributes("-alpha", tgt)
                    except Exception:
                        pass
                    if tgt <= 0.001 and not state["was_visible"]:
                        try:
                            root.withdraw()
                        except Exception:
                            pass
                return
            step = 0.12 if tgt > cur else -0.12
            new = cur + step
            if (step > 0 and new > tgt) or (step < 0 and new < tgt):
                new = tgt
            state["current_alpha"] = new
            try:
                root.attributes("-alpha", max(0.0, min(1.0, new)))
            except Exception:
                pass

        # ---- Aktivitaets-Detection (verbessert nach Codex-Analyse) ----
        # Status-Check jetzt mtime-getrieben: bei neuer mtime sofort pruefen,
        # sonst throttlen. State-Stabilisierung: "coding" bleibt stabil bei
        # frischer Aktivitaet, kurze Zwischenzustaende unterbrechen nicht.
        state["last_status_check"] = 0.0
        state["last_known_mtime"] = 0.0
        state["is_limited"] = False
        state["limit_type"] = None
        state["limited_until"] = 0.0  # Hysterese: Limit bleibt bis hier
        state["is_waiting"] = False
        state["is_awaiting_approval"] = False
        state["last_block_type"] = None
        state["last_tool_name"] = None
        state["internal_state"] = "no_session"
        # State-Stabilisierung: coding/tool_running bleibt stabil
        state["stable_state"] = None
        state["stable_since"] = 0.0
        state["last_frame_at"] = 0.0  # Frame-Rate getrennt von State-Polling
        state["hover_started_at"] = 0.0
        state["wink_until"] = 0.0
        state["last_alt_swap"] = 0.0
        state["use_alt"] = False

        # States die "coding" blockieren duerfen (hohe Prioritaet)
        _HIGH_PRIO_STATES = {"done", "tool_pending_approval", "rate_limited",
                            "auth_required", "api_overloaded", "no_session"}
        # States die als "aktiv arbeitend" gelten
        _WORKING_STATES = {"tool_running", "processing_tool_result",
                          "responding_text", "thinking", "user_sent_prompt"}

        def detect_state():
            now = time.time()
            # mtime-Check: immer schnell (alle 500ms)
            if now - state["last_mtime_check"] > 0.5:
                state["last_mtime_check"] = now
                pdir = ""
                try:
                    pdir = self.api._projects_dir()
                except Exception:
                    pdir = ""
                new_mtime = _latest_session_mtime(pdir)
                mtime_changed = new_mtime != state["last_known_mtime"]
                state["last_known_mtime"] = new_mtime
                state["last_mtime"] = new_mtime

                # Status-Check: sofort bei mtime-Aenderung, sonst max alle 2s
                should_check = mtime_changed or (now - state["last_status_check"] > 2.0)

                if new_mtime > 0 and should_check:
                    state["last_status_check"] = now
                    try:
                        status = _latest_jsonl_status(pdir)
                    except Exception:
                        status = {"internal_state": "no_session", "is_limit": False}

                    # Limit mit Hysterese: bleibt aktiv bis reset_at oder 60s
                    new_limited = bool(status.get("is_limit"))
                    new_limit_type = status.get("limit_type")
                    reset_at = float(status.get("reset_at") or 0.0)

                    if new_limited:
                        state["is_limited"] = True
                        state["limit_type"] = new_limit_type
                        if reset_at > 0:
                            state["limited_until"] = reset_at
                        else:
                            # Kein reset_at: 60s Hysterese
                            state["limited_until"] = now + 60
                        # Reset-Zeit speichern
                        try:
                            prev = float(self.api.settings.get("limit_reset_at", 0) or 0)
                            if reset_at > 0 and abs(prev - reset_at) > 30:
                                self.api.settings["limit_reset_at"] = reset_at
                                save_json(SETTINGS_FILE, self.api.settings)
                                self._schedule_reset_timer(reset_at)
                        except Exception:
                            pass
                    elif state["is_limited"]:
                        # Limit aufheben nur wenn Hysterese abgelaufen
                        if now > state["limited_until"]:
                            # Erfolgreiche neue Aktivitaet -> Limit aufheben
                            int_state = status.get("internal_state", "")
                            if int_state in _WORKING_STATES or int_state == "done":
                                try:
                                    self._notify_limit_reset()
                                except Exception:
                                    pass
                                state["is_limited"] = False
                                state["limit_type"] = None

                    state["is_waiting"] = bool(status.get("waiting"))
                    state["is_awaiting_approval"] = bool(status.get("awaiting_approval"))
                    state["last_block_type"] = status.get("last_block_type")
                    state["last_tool_name"] = status.get("last_tool_name")
                    state["internal_state"] = status.get("internal_state", "unknown_active")

            if state["last_mtime"] <= 0:
                return "none"

            age = now - state["last_mtime"]
            int_state = state.get("internal_state", "unknown_active")

            # HIGH-PRIO States: sofort anzeigen
            if state["is_limited"] and age < 3600:
                return "limit"
            if state["is_awaiting_approval"] and age < 300:
                return "awaiting_approval"
            if int_state == "done" and age < 180:
                return "waiting"

            # Arbeitszustaende. Frueher galt hier: einmal "active", und die
            # naechsten FUENF MINUTEN blieb es dabei, egal was Claude
            # tatsaechlich tat. Das sollte Flackern verhindern, war als Mittel
            # aber viel zu grob - Clawd stand minutenlang auf "arbeitet",
            # waehrend Claude laengst nachdachte oder Text schrieb. Genau
            # deshalb passten die Animationen so oft nicht.
            #
            # Jetzt folgt der Zustand dem Geschehen; gegen Flackern reicht die
            # kurze Standzeit (_STATE_DEBOUNCE_S), die weiter unten beim
            # Anim-Wechsel greift.
            if int_state in _WORKING_STATES and age < 300:
                if state["stable_state"] != int_state:
                    state["stable_state"] = int_state
                    state["stable_since"] = now
                if int_state in ("thinking", "user_sent_prompt"):
                    return "thinking"
                if int_state == "tool_pending_approval":
                    return "awaiting_approval"
                return "active"

            # Nicht mehr aktiv arbeitend - stable state zuruecksetzen
            if int_state not in _WORKING_STATES:
                state["stable_state"] = None
                state["stable_since"] = 0.0

            # DONE vs IDLE: done nur bei end_turn, idle durch Zeit
            if state["is_waiting"] and age < 180:
                return "waiting"
            if age < 300:
                return "recent"
            if age < 900:
                return "idle"
            return "sleep"

        # ---- Anim wechseln ----
        def choose_anim():
            bud = self.api.settings.get("buddy", {})
            now = time.time()
            # Preview-Test aus dem Buddy-Tab hat hoechste Prioritaet
            if now < state["preview_until"] and state["preview_anim"] in BUDDY_ANIMS:
                return state["preview_anim"]
            # Surprise-Pulse (z.B. Reset-Karte oder Test-Button)
            if now < state["surprise_until"]:
                return BUDDY_STATE_MAP["surprise"]
            # Wink-Easter-Egg: ein Mal pro Hover-Session zwinkern nach 10s
            # Dwell. Weiter zwinkern erst nach neuem Mouse-Leave/Enter.
            if state.get("hover") and state["hover_started_at"] > 0 \
                    and not state.get("wink_fired_this_hover"):
                dwell = now - state["hover_started_at"]
                if dwell > 10:
                    state["wink_fired_this_hover"] = True
                    state["wink_until"] = now + 2.5
            if now < state["wink_until"]:
                return BUDDY_STATE_MAP["wink"]
            # Party-Modus mit waehlbarem Stil (bounce oder sway)
            if bud.get("party"):
                style = str(bud.get("party_style", "bounce")).lower()
                if style == "sway":
                    return BUDDY_STATE_MAP["party_sway"]
                return BUDDY_STATE_MAP["party"]
            act = detect_state()
            state["activity_state"] = act
            # Ein State = eine Anim, so lang der State anhaelt. Kein Rotieren
            # zwischen "work coding" und "write" alle 30s mehr - der User hat
            # zu Recht gesagt: wenn Claude 10 Min codet soll auch 10 Min
            # "work coding" laufen. Weniger Bewegung im Augenwinkel.
            # ("write" und "think" sind weiter ueber die Preview-Kachel im
            # Buddy-Tab antestbar, nur nicht mehr in der Auto-Rotation.)
            return BUDDY_STATE_MAP.get(act, "idle breathe")

        # ---- Rendering (mit Frame-Cache) ----
        render_cache = {}
        last_drawn = {"key": None}

        def render_frame():
            name = state["anim"]
            anim = BUDDY_ANIMS.get(name) or next(iter(BUDDY_ANIMS.values()))
            frames = anim["frames"]
            if not frames:
                return
            frame_idx = state["frame"] % len(frames)
            sc = state["scale"]
            # Ohne Rahmen wird die Hintergrundflaeche durchsichtig - Windows
            # blendet den Chroma-Key aus, uebrig bleibt das Motiv. Mit Rahmen
            # bleibt die dunkle Flaeche stehen; ein Rahmen um nichts herum
            # saehe seltsam aus.
            #
            # Beim Platzieren wieder deckend: Windows laesst Klicks durch die
            # durchsichtigen Stellen hindurch. Ohne diese Ausnahme liesse sich
            # der Buddy nur noch am Koerper anfassen, und genau dort will man
            # ihn beim Verschieben am wenigsten treffen muessen.
            bg_fill = (self._TRANSPARENT
                       if (state["frame_style"] == "off" and not state["placing"])
                       else "#14100e")
            key = (name, frame_idx, sc, bg_fill)

            if key == last_drawn["key"]:
                state["frame"] += 1
                return

            rows_data = render_cache.get(key)
            if rows_data is None:
                palette = anim["palette"]
                f = frames[frame_idx]
                # 0 ist „nicht gesetzt", der zweite Wert ist die eigene
                # Hintergrundflaeche des Sprites - beides zaehlt als leer.
                hg_idx = _hintergrund_index(f)
                rows_data = []
                for row in range(20):
                    cells = []
                    ridx = row * 20
                    for col in range(20):
                        idx = f[ridx + col]
                        if idx <= 0 or idx == hg_idx:
                            cells.append(bg_fill)
                        elif idx < len(palette):
                            cells.append(palette[idx])
                        else:
                            cells.append(bg_fill)
                    row_str = "{" + " ".join(
                        (" ".join([c] * sc)) for c in cells) + "}"
                    rows_data.append(row_str)
                if len(render_cache) > 400:
                    render_cache.clear()
                render_cache[key] = rows_data

            try:
                for row in range(20):
                    y1 = row * sc
                    img.put(rows_data[row], to=(0, y1, 20 * sc, y1 + sc))
                last_drawn["key"] = key
            except Exception:
                pass
            state["frame"] += 1

        # ---- Command-Queue ----
        def process_cmds():
            try:
                while True:
                    cmd, val = self._q.get_nowait()
                    if cmd == "quit":
                        self._alive = False
                        try:
                            root.destroy()
                        except Exception:
                            pass
                        return False
                    elif cmd == "refresh":
                        # Buddy-Einstellungen neu einlesen. Wer an den
                        # Einstellungen dreht will ihn sehen -> Snooze weg.
                        state["snooze_keys"] = None
                        state["snooze_empty_since"] = 0.0
                        new = self.api.settings.get("buddy", {})
                        new_scale = max(2, min(10, int(new.get("size", 4))))
                        new_op = max(20, min(100, int(new.get("opacity", 100)))) / 100.0
                        new_style = _resolved_frame_style(new)
                        new_color = new.get("frame_color") or "#ec7456"
                        new_label = new.get("frame_label") or "CLAWD"
                        # Sprite-/Fenster-/Frame-Rebuild wenn irgendwas
                        # dimensions- oder styleaenderndes anliegt.
                        if (new_scale != state["scale"] or
                                new_style != state["frame_style"] or
                                new_color != state["frame_color"] or
                                new_label != state["frame_label"]):
                            state["scale"] = new_scale
                            rebuild_frame(new_style, new_color, new_label, new_scale)
                        if abs(new_op - state["opacity"]) > 0.001:
                            state["opacity"] = new_op
                            if state["was_visible"]:
                                state["target_alpha"] = new_op
                        _visible["v"] = None
                    elif cmd == "hide_toggle":
                        # Doppel-/Rechtsklick -> nur wegschnoozen. Der Buddy
                        # bleibt in den Settings aktiviert und der Thread
                        # laeuft weiter; er kommt beim naechsten neuen
                        # Claude-Terminal von selbst zurueck.
                        state["snooze_keys"] = _claude_context_keys()
                        state["snooze_empty_since"] = 0.0
                        _visible["v"] = None
                    elif cmd == "pulse":
                        state["surprise_until"] = time.time() + 1.6
                    elif cmd == "place":
                        state["placing"] = True
                        state["place_pulse"] = 0.0
                        try:
                            canvas.itemconfigure(place_ring, state="normal")
                            root.attributes("-topmost", True)
                        except Exception:
                            pass
                        # Vollflaechen-Overlay mit Grid einblenden
                        if state.get("overlay") is None:
                            state["overlay"] = build_overlay()
                    elif cmd == "preview":
                        name, seconds = val
                        if name in BUDDY_ANIMS:
                            state["preview_anim"] = name
                            state["preview_until"] = time.time() + seconds
                    elif cmd == "jump":
                        nx, ny = val
                        # Fenstergroesse inkl. Rahmen (state["px_w"]) fuer korrektes
                        # Edge-Snap, nicht nur Sprite-Groesse.
                        size_px = state.get("px_w", 20 * state["scale"])
                        nx, ny = _snap_position(nx, ny, size_px)
                        try:
                            root.geometry(f"+{nx}+{ny}")
                        except Exception:
                            pass
                        bud = self.api.settings.setdefault("buddy", {})
                        bud["x"], bud["y"] = nx, ny
                        try:
                            save_json(SETTINGS_FILE, self.api.settings)
                        except Exception:
                            pass
            except queue.Empty:
                pass
            return True

        # ---- Haupt-Loop (via after()) ----
        def tick():
            if not self._alive:
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            state["tick"] += 1
            if not process_cmds():
                return
            apply_visibility()
            step_fade()
            chosen = choose_anim()
            # Debounce: nicht bei jedem 300ms-Tick zwischen Anims flackern.
            # Neue Ziel-Anim muss _STATE_DEBOUNCE_S lang stabil gewuenscht sein
            # bevor gewechselt wird. Ausnahmen (sofort schalten): high-priority
            # Signale wie allow/limit/surprise/wink/preview - der User soll sie
            # ohne Verzoegerung sehen.
            _priority = {"limit", "allow", "expression surprise",
                         "expression wink"}
            now = time.time()
            if chosen != state["anim"]:
                if chosen in _priority or state.get("preview_until", 0) > now:
                    state["anim"] = chosen
                    state["frame"] = 0
                    state["last_frame_at"] = 0.0
                    state["pending_anim"] = None
                    state["pending_since"] = 0.0
                else:
                    if state.get("pending_anim") != chosen:
                        state["pending_anim"] = chosen
                        state["pending_since"] = now
                    elif now - state["pending_since"] >= self._STATE_DEBOUNCE_S:
                        state["anim"] = chosen
                        state["frame"] = 0
                        state["pending_anim"] = None
                        state["pending_since"] = 0.0
            else:
                # Ziel-Anim = aktuelle Anim -> pending zuruecksetzen
                if state.get("pending_anim") is not None:
                    state["pending_anim"] = None
                    state["pending_since"] = 0.0
            self._pub_anim = state["anim"]
            self._pub_limit = (bool(state.get("is_limited")),
                               float(state.get("limited_until") or 0.0))
            # Wo der Buddy steht, zum Mitlesen: die Reset-Karte kommt oben
            # rechts herein und wuerde sonst unter ihm landen. Bewusst
            # unabhaengig davon, ob er gerade eingeblendet ist - dieselbe
            # Meldung weckt ihn ja auf ("surprise"), er waere also Sekunden
            # spaeter doch da. Der Platz gehoert ihm, auch wenn er gerade
            # nicht hinschaut.
            rect = None
            try:
                rect = (root.winfo_x(), root.winfo_y(),
                        state.get("px_w", px_w), state.get("px_h", px_h))
            except Exception:
                pass
            self._pub_rect = rect

            # Haengengebliebenes Hover aufloesen. Durchsichtig wird der Buddy
            # ueber <Enter>, zurueck ueber <Leave> - und <Leave> kommt nicht
            # zuverlaessig an, wenn ihn ein anderes Fenster ueberdeckt oder die
            # Maus in einem Rutsch aus dem Bild faehrt. Dann stuende er auf
            # Dauer-15%. Die Mausposition weiss es besser als das Ereignis.
            if state.get("hover") and rect:
                try:
                    mx, my = root.winfo_pointerxy()
                    rx, ry, rw, rh = rect
                    if not (rx <= mx < rx + rw and ry <= my < ry + rh):
                        _on_leave(None)
                except Exception:
                    pass
            # Frame-Rate getrennt von tick-Rate: Frame-Advance nur alle
            # _FRAME_MS, aber tick bleibt schnell fuer Visibility/Fade/Hover.
            if state["current_alpha"] > 0.01:
                if now - state["last_frame_at"] >= self._FRAME_MS / 1000.0:
                    render_frame()
                    state["last_frame_at"] = now
            # Place-Mode: Rahmen pulsieren fuer bessere Sichtbarkeit
            if state["placing"]:
                state["place_pulse"] = (state["place_pulse"] + 0.14) % 6.283
                import math
                intensity = int(2 + 3 * (0.5 + 0.5 * math.sin(state["place_pulse"])))
                try:
                    canvas.itemconfigure(place_ring, width=intensity)
                except Exception:
                    pass
            root.after(self._TICK_MS, tick)

        try:
            root.after(50, tick)
            root.mainloop()
        except Exception:
            pass
        finally:
            self._alive = False
            self._pub_rect = None


# --------------------------------------------------------------------------- #
#  Limit-Reset-Karte: schwebende Karte oben rechts, bleibt bis Klick weg
# --------------------------------------------------------------------------- #
def _dodge_y(x, y, w, h, avoid, screen_h, gap=14, margin=8):
    """Schiebt ein Fenster senkrecht aus `avoid` heraus.

    Gedacht fuer die Reset-Karte, die oben rechts hereinkommt - genau dort, wo
    viele ihren Buddy stehen haben. Der Buddy steht, wo der Nutzer ihn
    hingestellt hat, also weicht die Karte aus: erst darunter, sonst darueber.
    Passt beides nicht, bleibt es beim alten Platz - dann ist der Bildschirm
    ohnehin zu klein fuer beides.
    """
    if not avoid:
        return y
    try:
        ax, ay, aw, ah = (int(v) for v in avoid)
    except (TypeError, ValueError):
        return y
    if aw <= 0 or ah <= 0:
        return y
    if not (ax < x + w and ax + aw > x and ay < y + h and ay + ah > y):
        return y
    below = ay + ah + gap
    if below + h <= screen_h - margin:
        return below
    above = ay - h - gap
    if above >= margin:
        return above
    return y


class LimitResetToast:
    """Zeigt eine Anthropic-Style-Karte oben rechts am Bildschirm die den User
    darueber informiert dass sein Claude-Limit zurueckgesetzt wurde. Karte
    bleibt sichtbar bis irgendwo drauf geklickt wird (nicht verpassbar).
    Laeuft in eigenem Daemon-Thread mit eigenem tkinter-Root."""

    # Anthropic-Palette (warmes Off-Cream + tiefes warmes Braun)
    _BG_KEY = "#ff00fe"          # Chroma-Key fuer echte Rundungen
    _CARD = "#f5f2eb"            # warmes Off-Cream (Anthropic-Papier)
    _CARD_HOVER = "#efeadf"
    _ACCENT = "#d97757"          # Claude-Orange
    _ACCENT_SOFT = "#e8a889"     # heller Ring
    _TITLE = "#1a1815"           # tiefes warmes Braun
    _SUBTLE = "#6b6660"
    _CLOSE = "#8a857f"
    _CLOSE_HOVER = "#1a1815"

    def __init__(self):
        self._alive = False
        self._t = None

    def show(self, title=None, subtitle=None, avoid=None):
        # Erst hier uebersetzen, nicht als Standardwert im Kopf: Standardwerte
        # werden beim Import ausgewertet, da steht die Sprache noch nicht fest.
        # `avoid` ist ein Rechteck (x, y, w, h), das die Karte freilassen soll -
        # der Buddy steht bei vielen oben rechts, genau im Anflugweg.
        title = title or t("Dein Claude-Limit ist zurückgesetzt")
        subtitle = subtitle or t("Du kannst weitermachen")
        if self._alive:
            return
        self._alive = True
        self._t = threading.Thread(
            target=self._run, args=(title, subtitle, avoid), daemon=True)
        self._t.start()
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    def _rounded_rect(self, cv, x1, y1, x2, y2, r, fill, outline=""):
        """Zeichnet ein Rechteck mit runden Ecken auf einen Canvas."""
        pts = [
            x1 + r, y1,  x2 - r, y1,
            x2, y1,  x2, y1 + r,
            x2, y2 - r,  x2, y2,
            x2 - r, y2,  x1 + r, y2,
            x1, y2,  x1, y2 - r,
            x1, y1 + r,  x1, y1,
        ]
        return cv.create_polygon(pts, smooth=True, fill=fill,
                                 outline=outline)

    def _run(self, title, subtitle, avoid=None):
        try:
            import tkinter as tk
        except Exception:
            self._alive = False
            return
        try:
            root = tk.Tk()
        except Exception:
            self._alive = False
            return

        root.withdraw()
        try:
            root.title("Claude")
        except Exception:
            pass
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", self._BG_KEY)
        except Exception:
            pass
        try:
            root.attributes("-alpha", 0.0)
        except Exception:
            pass
        root.configure(bg=self._BG_KEY)

        # Card selbst ohne Schatten-Rand: Fenster = Card-Bounds
        W, H = 300, 78
        try:
            sw = root.winfo_screenwidth()
        except Exception:
            sw = 1920
        target_x = sw - W - 28
        target_y = 40
        try:
            sh = root.winfo_screenheight()
        except Exception:
            sh = 1080
        target_y = _dodge_y(target_x, target_y, W, H, avoid, sh)
        start_x = sw + 20
        root.geometry(f"{W}x{H}+{start_x}+{target_y}")

        cv = tk.Canvas(root, width=W, height=H,
                       bg=self._BG_KEY, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)

        state = {"closing": False, "hover": False,
                 "card_id": None, "close_id": None,
                 "close_bg_id": None, "title_id": None, "sub_id": None,
                 "logo_ring": None, "logo_dot": None, "logo_star": None}

        def _redraw(hover):
            cv.delete("all")
            card_fill = self._CARD_HOVER if hover else self._CARD
            # Karte mit Rundungen
            state["card_id"] = self._rounded_rect(
                cv, 0, 0, W, H, 12, fill=card_fill)
            # Kleiner cleaner Orange-Dot links (kein Text/Icon drin)
            cx, cy = 24, H // 2
            r_outer, r_inner = 9, 5
            cv.create_oval(cx - r_outer, cy - r_outer,
                           cx + r_outer, cy + r_outer,
                           fill=self._ACCENT_SOFT, outline="")
            cv.create_oval(cx - r_inner, cy - r_inner,
                           cx + r_inner, cy + r_inner,
                           fill=self._ACCENT, outline="")
            # Text-Block
            tx = 44
            cv.create_text(
                tx, 26, text=title, anchor="w",
                font=("Segoe UI Semibold", 10), fill=self._TITLE)
            cv.create_text(
                tx, 48, text=subtitle, anchor="w",
                font=("Segoe UI", 9), fill=self._SUBTLE)
            # Close-Button oben rechts
            close_col = self._CLOSE_HOVER if hover else self._CLOSE
            cv.create_text(
                W - 14, 12, text="✕", font=("Segoe UI", 9),
                fill=close_col)

        _redraw(False)

        def _do_close(_e=None):
            if state["closing"]:
                return
            state["closing"] = True

            def fade(step=10):
                if step <= 0:
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    return
                try:
                    root.attributes("-alpha", step / 10.0)
                except Exception:
                    pass
                root.after(20, lambda: fade(step - 1))
            fade(10)

        def _hover_on(_e=None):
            if state["closing"] or state["hover"]:
                return
            state["hover"] = True
            _redraw(True)

        def _hover_off(_e=None):
            if state["closing"] or not state["hover"]:
                return
            state["hover"] = False
            _redraw(False)

        cv.bind("<Button-1>", _do_close)
        cv.bind("<Enter>", _hover_on)
        cv.bind("<Leave>", _hover_off)
        try:
            cv.configure(cursor="hand2")
        except Exception:
            pass

        try:
            root.deiconify()
        except Exception:
            pass

        anim = {"step": 0, "steps": 14}

        def slide():
            if state["closing"]:
                return
            anim["step"] += 1
            t = anim["step"] / anim["steps"]
            e = 1 - (1 - t) ** 3
            nx = int(start_x + (target_x - start_x) * e)
            try:
                root.geometry(f"{W}x{H}+{nx}+{target_y}")
                root.attributes("-alpha", min(1.0, t * 1.15))
            except Exception:
                return
            if anim["step"] < anim["steps"]:
                root.after(16, slide)

        root.after(16, slide)

        try:
            root.mainloop()
        except Exception:
            pass
        finally:
            self._alive = False


# --------------------------------------------------------------------------- #
#  System-Tray (X = App in Hintergrund)
# --------------------------------------------------------------------------- #
class TrayManager:
    """System-Tray-Icon damit die App im Hintergrund weiterlaeuft wenn der
    User auf X klickt. Rechtsklick → Menue mit Oeffnen/Beenden. Linksklick
    → App wieder zeigen."""

    def __init__(self, get_window, on_quit):
        self.get_window = get_window
        self.on_quit = on_quit
        self.icon = None
        self._thread = None

    def start(self):
        if self.icon:
            return
        try:
            import pystray
            from PIL import Image
        except Exception:
            return

        icon_img = None
        # Reihenfolge: bevorzugt .ico (App-Icon, immer im Build), dann logo.png,
        # dann farbiges Fallback-Quadrat.
        for candidate in ("claude_sessions.ico", "logo.png"):
            try:
                icon_img = Image.open(_resource(candidate))
                break
            except Exception:
                continue
        if icon_img is None:
            try:
                icon_img = Image.new("RGB", (64, 64), "#ec7456")
            except Exception:
                return

        def _open(icon, item):
            self.show_main()

        def _quit(icon, item):
            try:
                self.icon.stop()
            except Exception:
                pass
            try:
                self.on_quit()
            except Exception:
                pass

        # Beschriftung als Funktion, nicht als fester Text: pystray fragt sie
        # beim Aufklappen ab, damit stimmt das Menue sofort nach einem
        # Sprachwechsel - ohne das Icon neu aufbauen zu muessen.
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: t("Öffnen"), _open, default=True),
            pystray.MenuItem(lambda item: t("Beenden"), _quit),
        )
        self.icon = pystray.Icon(
            "ClaudeSessionBrowser",
            icon=icon_img,
            title="Claude Session Browser",
            menu=menu,
        )
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="TrayThread")
        self._thread.start()

    def _run(self):
        try:
            self.icon.run()
        except Exception:
            pass

    def show_main(self):
        win = self.get_window()
        if not win:
            return
        try:
            win.show()
        except Exception:
            pass
        try:
            win.restore()
        except Exception:
            pass
        # Notausgang: liegt das Fenster ausserhalb jedes Bildschirms (Monitor
        # abgesteckt, Anordnung geaendert), holt "Oeffnen" es in die Mitte
        # zurueck. Sonst waere es ueber das Tray-Menue nicht erreichbar.
        try:
            w, h = int(win.width or 1180), int(win.height or 760)
            if not _position_is_usable(int(win.x), int(win.y), w, h):
                win.move(max(0, (_screen_w() - w) // 2),
                         max(0, (_screen_h() - h) // 2))
        except Exception:
            pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  API (von JavaScript aufrufbar)
# --------------------------------------------------------------------------- #
def _png_rgba(width, height, rows):
    """Minimales RGBA-PNG als data-URI. `rows` ist eine Liste von bytearrays
    (je 4*width Bytes). Pillow waere hier ein Import zu viel - PNG mit zlib
    selbst zu schreiben sind zwanzig Zeilen."""
    raw = bytearray()
    for r in rows:
        raw.append(0)          # Filter "None" pro Zeile
        raw += r

    def chunk(tag, data):
        out = len(data).to_bytes(4, "big") + tag + data
        return out + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")

    ihdr = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes((8, 6, 0, 0, 0)))       # 8 bit, Farbtyp 6 = RGBA
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _sprite_icon_png(anim, box_px=40):
    """Erster Frame einer Animation, auf die Figur zugeschnitten, quadratisch
    aufgefuellt und mit durchsichtigem Rand.

    Zugeschnitten wird gegen zwei "leere" Werte: Index 0 (nie gesetzt) und den
    Wert der linken oberen Ecke - das ist bei diesen Sprites die schwarze
    Hintergrundflaeche. Die Ausmasse werden ueber ALLE Frames bestimmt, sonst
    wackelt das Bild je nachdem welcher Frame gerade dran ist.

    Quadratisch aufgefuellt statt auf die Kachel gedehnt: die Figur ist
    breiter als hoch, gedehnt saehe sie gequetscht aus.

    Das Bild wird in genau der Kantenlaenge geliefert, in der es auch
    angezeigt wird (`box_px`), und die Figur darin mit GANZZAHLIGEM Faktor
    vergroessert. Ein hoeher aufgeloestes Bild per CSS herunterzurechnen sah
    matschig aus - bei Pixelgrafik zerfaellt jede krumme Skalierung, aus
    108 px auf 34 wird Brei.
    """
    if not anim or not anim.get("frames"):
        return ""
    frames, palette = anim["frames"], anim["palette"]
    empty = {0, frames[0][0]}
    xs, ys = [], []
    for f in frames:
        for i, v in enumerate(f):
            if v not in empty:
                xs.append(i % 20)
                ys.append(i // 20)
    if not xs:
        return ""
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1

    scale = max(1, box_px // max(bw, bh))     # ganzzahlig, sonst wird es Brei
    mw, mh = bw * scale, bh * scale
    ox, oy = (box_px - mw) // 2, (box_px - mh) // 2   # im Kaesten zentrieren

    frame = frames[0]
    leer = bytes((0, 0, 0, 0))
    rows = []
    for py in range(box_px):
        row = bytearray()
        gy = (py - oy) // scale + y0
        drin_y = oy <= py < oy + mh
        for px_ in range(box_px):
            gx = (px_ - ox) // scale + x0
            if not (drin_y and ox <= px_ < ox + mw):
                row += leer
                continue
            v = frame[gy * 20 + gx]
            if v in empty:
                row += leer
            else:
                hx = (palette[v] if v < len(palette) else "#000000").lstrip("#")
                row += bytes((int(hx[0:2], 16), int(hx[2:4], 16),
                              int(hx[4:6], 16), 255))
        rows.append(row)
    return _png_rgba(box_px, box_px, rows)


class Api:
    # Wie lange eine Ratelimit-Messung als aktuell gilt. Der Watcher fragt
    # alle 5 Minuten; drei Intervalle Luft, damit ein einzelner Fehlschlag
    # die Anzeige nicht sofort auf die schlechtere Quelle wirft.
    _LIMIT_HEADER_TRUST_S = 15 * 60

    def __init__(self):
        self.overrides = load_json(TITLES_FILE, {})
        self.settings = load_settings()
        self._cache = None
        self._current_view = "sessions"
        self.buddy = BuddyController(self)
        self._reset_toast = None
        # Startup-Nachhol: Reset-Zeit in der Vergangenheit + noch nicht
        # benachrichtigt → Karte jetzt nachtraeglich zeigen. Aussderdem
        # zukuenftige Reset-Zeit als Timer registrieren.
        try:
            self._check_pending_limit_reset()
        except Exception:
            pass

    def _check_pending_limit_reset(self):
        reset_at = float(self.settings.get("limit_reset_at", 0) or 0)
        if reset_at <= 0:
            return
        notified_for = float(
            self.settings.get("limit_reset_notified_for", 0) or 0)
        if abs(notified_for - reset_at) < 30:
            return
        now = time.time()
        if reset_at <= now:
            # Reset ist in der Vergangenheit, User war offline. Kurz
            # verzoegert triggern damit UI erst hochkommt.
            def _late_fire():
                try:
                    self.buddy._notify_limit_reset()
                except Exception:
                    pass
            t = threading.Timer(3.0, _late_fire)
            t.daemon = True
            t.start()
        else:
            # Reset noch in Zukunft → Timer registrieren
            try:
                self.buddy._schedule_reset_timer(reset_at)
            except Exception:
                pass

    @staticmethod
    def _win():
        return webview.windows[0] if webview.windows else None

    def bind_window(self, win):
        """Merkt sich Fenstergroesse/-position/Maximierung – ressourcenschonend:
        waehrend der Nutzung nur In-Memory, gespeichert wird nur beim Schliessen."""
        s = self.settings
        self._max = bool(s.get("win_max"))
        self._geo = {
            "w": s.get("win_w") or 1180, "h": s.get("win_h") or 760,
            "x": s.get("win_x"), "y": s.get("win_y"),
        }

        def on_resized(*a):
            if len(a) >= 2 and not self._max:
                self._geo["w"], self._geo["h"] = a[0], a[1]

        def on_moved(*a):
            # Windows meldet fuer ein minimiertes Fenster -32000/-32000. Wer
            # die App minimiert und in dem Zustand beendet, haette sonst diese
            # Position gespeichert - und beim naechsten Start ein Fenster
            # ausserhalb jedes Bildschirms: Eintrag in der Taskleiste, aber
            # nichts zu sehen, und das dauerhaft.
            if len(a) >= 2 and not self._max:
                if a[0] <= -30000 or a[1] <= -30000:
                    return
                self._geo["x"], self._geo["y"] = a[0], a[1]

        def on_max(*a):
            self._max = True

        def on_restore(*a):
            self._max = False

        def on_closing(*a):
            if getattr(self, "_geo_saved", False):
                return   # nur einmal speichern (closing UND closed feuern)
            self._geo_saved = True
            self.settings["win_w"] = int(self._geo["w"])
            self.settings["win_h"] = int(self._geo["h"])
            if self._geo["x"] is not None:
                self.settings["win_x"] = int(self._geo["x"])
            if self._geo["y"] is not None:
                self.settings["win_y"] = int(self._geo["y"])
            self.settings["win_max"] = bool(self._max)
            save_json(SETTINGS_FILE, self.settings)

        win.events.resized += on_resized
        win.events.moved += on_moved
        win.events.maximized += on_max
        win.events.restored += on_restore
        win.events.closing += on_closing
        win.events.closed += on_closing   # Fallback, falls 'closing' nicht feuert

    # -- intern --
    def _projects_dir(self):
        p = self.settings.get("projects_dir")
        if p and os.path.isdir(p):
            return p
        return detect_projects_dir()

    def _sessions(self, force=False):
        if self._cache is None or force:
            self._cache = collect_sessions(self._projects_dir())
        colors = self.settings.get("session_colors", {})
        for s in self._cache:
            s["display_title"] = self.overrides.get(s["id"], s["auto_title"])
            s["color"] = colors.get(s["id"], "")
            s["when"] = fmt_time(s["mtime"])
        return self._cache

    def _state(self, force=False):
        pdir = self._projects_dir()
        return {
            "sessions": self._sessions(force),
            "settings": self.settings,
            "projects_dir": pdir,
            "found": bool(pdir and os.path.isdir(pdir)),
            "home": HOME,
            "version": VERSION,
            "onboarding_version": ONBOARDING_VERSION,
        }

    # -- von JS aufgerufen --
    def get_state(self):
        return self._state()

    def refresh(self):
        return self._state(force=True)

    def resume(self, sid, cwd, project=""):
        return resume_session(sid, cwd, self.settings, project)

    def rename(self, sid, title):
        title = (title or "").strip()
        auto = next((s["auto_title"] for s in (self._cache or []) if s["id"] == sid), "")
        if title and title != auto:
            self.overrides[sid] = title
        else:
            self.overrides.pop(sid, None)
        save_json(TITLES_FILE, self.overrides)
        return self._state()

    def set_color(self, sid, color):
        colors = self.settings.setdefault("session_colors", {})
        if color:
            colors[sid] = color
        else:
            colors.pop(sid, None)
        save_json(SETTINGS_FILE, self.settings)
        return self._state()

    def update_setting(self, key, value):
        force = False
        if key == "projects_dir":
            force = True
        self.settings[key] = value
        save_json(SETTINGS_FILE, self.settings)
        return self._state(force=force)

    def hooks_state(self):
        return {"on": hooks_eingerichtet(), "cmd": _hook_command()}

    def hooks_toggle(self, on):
        ok = hooks_einrichten(bool(on))
        return {"ok": ok, "on": hooks_eingerichtet()}

    def set_language(self, code):
        """Sprache umstellen. Gibt Sprache + Tabelle zurueck, damit die
        Oberflaeche sich sofort neu aufbauen kann - ohne Neustart."""
        if code not in ("auto", "de", "en"):
            code = "auto"
        self.settings["language"] = code
        lang = i18n.set_lang(code)
        save_json(SETTINGS_FILE, self.settings)
        return {"lang": lang, "table": i18n.table()}

    def add_hidden_folder(self, path):
        if path:
            folders = self.settings.setdefault("hidden_folders", [])
            if not any(norm(f) == norm(path) for f in folders):
                folders.append(path)
                save_json(SETTINGS_FILE, self.settings)
        return self._state()

    def remove_hidden_folder(self, path):
        folders = self.settings.get("hidden_folders", [])
        self.settings["hidden_folders"] = [f for f in folders if norm(f) != norm(path)]
        save_json(SETTINGS_FILE, self.settings)
        return self._state()

    def browse_folder(self):
        win = self._win()
        res = win.create_file_dialog(webview.FOLDER_DIALOG) if win else None
        if res:
            path = res[0] if isinstance(res, (list, tuple)) else res
            return self.update_setting("projects_dir", path)
        return self._state()

    def copy(self, text):
        try:
            subprocess.run(["clip"], input=str(text), text=True, shell=True)
            return True
        except OSError:
            return False

    # -- Buddy (Clawd-Maskottchen) --
    def buddy_state(self):
        """Was der Buddy-Tab braucht: aktuelle Config + verfuegbare Animationen
        (nur Namen; Sprites werden nicht ans UI geschickt) + Preview-Palette."""
        bud = self.settings.get("buddy", {})
        anims = []
        for name, data in BUDDY_ANIMS.items():
            frames = data.get("frames", [])
            anims.append({"name": name, "frames": len(frames)})
        # Preview-Frame als Data-URL fuer den Tab (aktuelle Animation, erstes
        # Frame – reicht als Icon).
        default_name = bud.get("preview_anim") or "idle breathe"
        preview = self._buddy_preview_gif(default_name)
        # Warum ist er ggf. gerade nicht sichtbar?
        reason = ""
        if bud.get("enabled") and self.buddy.is_alive():
            mode = bud.get("visibility", "when_claude")
            if mode == "when_claude" and not _claude_context_active():
                reason = "wartet auf Claude"
            elif mode == "when_window":
                needle = (bud.get("target_window") or "").lower().strip()
                fg = _win_foreground_title().lower()
                if needle and needle not in fg:
                    reason = "wartet auf Fenster"
        return {
            "config": bud,
            "anims": anims,
            "running": self.buddy.is_alive(),
            "reason": reason,
            "have_sprites": bool(BUDDY_ANIMS),
            "state_map": BUDDY_STATE_MAP,
            "preview": preview,
            "preview_name": default_name,
        }

    def buddy_set(self, key, value):
        bud = self.settings.setdefault("buddy", dict(DEFAULT_SETTINGS["buddy"]))
        bud[key] = value
        save_json(SETTINGS_FILE, self.settings)
        if key == "enabled":
            if value:
                self.buddy.start()
            else:
                self.buddy.stop()
        else:
            self.buddy.push(key)
        return self.buddy_state()

    def buddy_windows(self):
        """Aktuelle Fensterliste fuer den Picker."""
        return _win_list_windows()

    def buddy_surprise(self):
        self.buddy.surprise()
        return {"ok": True}

    # ---- Clawdmeter (BLE-Usage-Anzeige) --------------------------------

    def _clawd_anim(self):
        """Welche Animation soll das Clawdmeter zeigen?

        "" heisst: keine Vorgabe, das Geraet entscheidet nach Auslastung. Das
        ist auch der Fall wenn der Buddy selbst aus ist -- ohne laufenden
        Buddy gibt es keinen Zustand zu spiegeln."""
        if not self.settings.get("clawdmeter_buddy", True):
            return ""
        try:
            return self.buddy.current_anim()
        except Exception:
            return ""

    def _clawd_link(self):
        """Lazy-Init des BLE-Links. None wenn das Modul nicht verfuegbar ist."""
        link = getattr(self, "_clawdmeter", None)
        if link is not None:
            return link
        try:
            from clawdmeter import ClawdmeterLink
        except Exception:
            return None
        self._clawdmeter = ClawdmeterLink(
            address_provider=lambda: self.settings.get("clawdmeter_addr") or "",
            on_usage=self.on_usage_meta,
            anim_provider=self._clawd_anim,
            on_battery=self.on_clawd_battery)
        return self._clawdmeter

    def on_clawd_battery(self, pct):
        """Meldet sich einmal, wenn der Clawdmeter unter die Schwelle faellt.

        Die Sperre loest erst wieder aus, wenn der Ladestand die Schwelle
        deutlich ueberschreitet -- ohne diesen Abstand wuerde ein Wert, der um
        die Schwelle pendelt, dauernd neu melden. Beim Laden also einmal
        Ruhe, bis er wieder runter ist.
        """
        s = self.settings
        if not s.get("notify_clawd_battery", True):
            return
        warn_at = max(5, min(90, int(s.get("clawd_battery_pct", 15) or 15)))
        warned = bool(s.get("clawd_battery_warned"))

        if pct <= warn_at and not warned:
            s["clawd_battery_warned"] = True
            tray = getattr(self, "_tray", None)
            if tray and tray.icon:
                try:
                    tray.icon.notify(
                        t("Clawdmeter hat nur noch {pct}% Akku", pct=pct),
                        "Clawd")
                except Exception:
                    pass
        elif warned and pct >= warn_at + 10:
            s["clawd_battery_warned"] = False
        else:
            return
        try:
            save_json(SETTINGS_FILE, s)
        except Exception:
            pass

    # ---- Limit-Ueberwachung aus den Ratelimit-Headern -------------------

    def on_usage_meta(self, meta):
        """Wird nach jeder API-Abfrage gerufen (Clawdmeter-Link oder Watcher).

        Zwei Dinge passieren hier:
          1) Der echte Reset-Zeitpunkt aus den Headern loest die alte
             Schaetzung ab. Bisher kannte die App ihn nur, wenn Claude im
             JSONL eine Limit-Meldung hinterlassen hatte -- also erst NACHDEM
             man ins Limit gelaufen war. Jetzt steht er immer bereit.
          2) Ein Fensterwechsel wird erkannt und gemeldet -- aber nur wenn
             das abgelaufene Fenster ueberhaupt heiss war. Ohne diese Huerde
             kaeme alle fuenf Stunden eine Meldung, auch wenn man das Limit
             nie in die Naehe gebracht hat.
        """
        reset_at = float(meta.get("session_reset_at") or 0)
        pct = int(meta.get("session_pct") or 0)
        # Fuer die Anzeige in den Einstellungen festhalten. Absichtlich nur im
        # Speicher: der Wert ist Sekunden spaeter veraltet, in der
        # Einstellungsdatei waere er beim naechsten Start eine Luege.
        self._usage_meta = {"pct": pct, "reset_at": reset_at,
                            "wpct": int(meta.get("weekly_pct") or 0),
                            "wreset_at": float(meta.get("weekly_reset_at") or 0),
                            "at": time.time()}
        if reset_at <= 0:
            return
        s = self.settings
        warn_at = max(1, min(100, int(s.get("limit_warn_pct", 90) or 90)))
        window = float(s.get("limit_window_at", 0) or 0)
        peak = int(s.get("limit_window_peak", 0) or 0)
        dirty = False

        if abs(window - reset_at) > 60:
            # Neues Fenster. War das alte heiss, ist das Limit jetzt zurueck.
            if window > 0 and peak >= warn_at:
                s["limit_reset_at"] = window
                try:
                    self.buddy._notify_limit_reset()
                except Exception:
                    pass
            s["limit_window_at"] = reset_at
            s["limit_window_peak"] = pct
            peak = pct
            dirty = True
        elif pct > peak:
            s["limit_window_peak"] = pct
            peak = pct
            dirty = True

        # Vorwarnung, einmal pro Fenster.
        if (s.get("notify_limit_near", True) and peak >= warn_at
                and abs(float(s.get("limit_warned_for", 0) or 0) - reset_at) > 60):
            s["limit_warned_for"] = reset_at
            dirty = True
            self._notify_limit_near(pct, reset_at)

        if dirty:
            try:
                save_json(SETTINGS_FILE, self.settings)
            except Exception:
                pass

    def _notify_limit_near(self, pct, reset_at):
        """Tray-Meldung dass das 5h-Limit gleich voll ist."""
        mins = max(0, int((reset_at - time.time()) / 60))
        when = time.strftime("%H:%M", time.localtime(reset_at))
        tray = getattr(self, "_tray", None)
        if tray and tray.icon:
            try:
                tray.icon.notify(
                    t("{pct}% deines 5-Stunden-Limits verbraucht. "
                      "Zurückgesetzt um {when} – in {mins} Minuten.",
                      pct=pct, when=when, mins=mins), "Clawd")
            except Exception:
                pass

    def _kick_usage_poll(self):
        """Einmalige Ratelimit-Abfrage im Hintergrund.

        Jede Abfrage ist eine echte Anfrage an die API, deshalb hoechstens
        einmal pro Minute und nur wenn die Anzeige sie wirklich braucht. Der
        Aufrufer wartet nicht - der naechste Durchlauf der Seite (alle 20 s)
        findet das Ergebnis vor.
        """
        now = time.time()
        if now - float(getattr(self, "_usage_kick_at", 0) or 0) < 60:
            return
        self._usage_kick_at = now

        def _go():
            try:
                from clawdmeter import poll_usage_meta, read_token
                token = read_token()
                if not token:
                    return
                _payload, meta = poll_usage_meta(token)
                if meta:
                    self.on_usage_meta(meta)
            except Exception:
                pass

        threading.Thread(target=_go, daemon=True,
                         name="usage-kick").start()

    def limit_state(self):
        """Aktueller Stand des 5-Stunden-Limits fuer die Einstellungs-Seite.

        Zwei Quellen, und die Header gewinnen: sie wissen alles, was das
        Transcript weiss, und zusaetzlich alles darunter - den Unterschied
        zwischen 40% und 100%. Das Transcript kennt nur "voll" und weiss nicht,
        wann das aufhoert; eine Limit-Meldung von vorgestern sieht dort genauso
        aus wie eine von gerade eben. Als alleinige Quelle taugt es nur, wenn
        keine frische Messung vorliegt - kein Token, kein Netz, Abfrage aus.
        """
        now = time.time()
        meta = getattr(self, "_usage_meta", None) or {}
        pct = int(meta.get("pct") or 0)
        reset_at = float(meta.get("reset_at") or 0)
        stand_von = float(meta.get("at") or 0)

        if stand_von and (now - stand_von) < self._LIMIT_HEADER_TRUST_S:
            # Frische Messung. Sie entscheidet allein - auch darueber, ob das
            # Limit voll ist. Alles andere waere eine Zweitmeinung von etwas,
            # das weniger weiss. Ist der Reset-Zeitpunkt durch, ist nur das
            # Fenster gewechselt: die Prozentzahl gilt weiter, die naechste
            # Abfrage bringt den neuen Zeitpunkt.
            wreset = float(meta.get("wreset_at") or 0)
            return {"pct": pct, "reset_at": reset_at if reset_at > now else 0,
                    "hit": pct >= 100,
                    "wpct": int(meta.get("wpct") or 0),
                    "wreset_at": wreset if wreset > now else 0,
                    "known": True, "now": now}

        # Nichts Frisches da. Selbst eine holen - sonst haengt die Anzeige
        # davon ab, ob der Nutzer die Limit-Meldungen eingeschaltet hat, und
        # wer sie aus hat, saehe hier nie etwas.
        self._kick_usage_poll()

        # Bis die Antwort da ist: aufs Transcript zurueckfallen. Laeuft der
        # Buddy, hat er die Lage ohnehin im Blick; ist er aus, wird selbst
        # nachgesehen - die Anzeige darf nicht davon abhaengen, ob der Buddy
        # eingeschaltet ist.
        hit, until = False, 0.0
        if self.buddy.is_alive():
            hit, until = getattr(self.buddy, "_pub_limit", (False, 0.0))
        else:
            try:
                st = _latest_jsonl_status(self._projects_dir())
                hit = bool(st.get("is_limit"))
                until = float(st.get("reset_at") or 0)
            except Exception:
                pass
        # Eine Meldung, deren Reset-Zeit durch ist, beschreibt ein Fenster, das
        # es nicht mehr gibt. Ohne lesbare Reset-Zeit bleibt sie stehen - dann
        # ist sie das Einzige, was wir haben.
        if until and until <= now:
            hit = False
        reset_at = until if hit else 0.0
        # Der Wochenwert kommt nur aus den Headern. Ohne frische Messung waere
        # er derselbe alte Wert, der eben verworfen wurde - also weglassen; die
        # Oberflaeche blendet die Kachel dann aus.
        return {"pct": 100 if hit else 0, "reset_at": reset_at, "hit": hit,
                "wpct": 0, "wreset_at": 0,
                "known": bool(hit), "now": now}

    def clawdmeter_state(self):
        """Status fuer die Einstellungs-Seite."""
        on = bool(self.settings.get("clawdmeter"))
        addr = self.settings.get("clawdmeter_addr") or ""
        link = self._clawd_link()
        if link is None:
            return {"enabled": on, "available": False, "running": False,
                    "address": addr, "status": {}}
        return {"enabled": on, "available": True, "address": addr,
                "running": link.is_running(), "status": link.status()}

    def clawdmeter_devices(self, force=False):
        """Gekoppelte BLE-Geraete fuer die Auswahl-Liste."""
        try:
            import clawdmeter as cm
        except Exception:
            return {"ok": False, "devices": []}
        devices = cm.list_paired_devices(force=bool(force))
        auto = cm.discover_address(None)
        return {"ok": True, "devices": devices, "auto": auto or ""}

    def clawdmeter_reconnect(self):
        """Sofort neu verbinden, ohne die naechste Wartezeit abzusitzen."""
        if not self.settings.get("clawdmeter"):
            return self.clawdmeter_state()
        link = self._clawd_link()
        if link is not None:
            try:
                link.reconnect()
            except Exception:
                pass
        return self.clawdmeter_state()

    def clawdmeter_pick(self, address):
        """Geraet festlegen (leer = wieder automatisch suchen)."""
        self.settings["clawdmeter_addr"] = (address or "").strip().upper()
        save_json(SETTINGS_FILE, self.settings)
        link = self._clawd_link()
        # Laufende Verbindung neu aufbauen, damit die Wahl sofort greift.
        if link is not None and self.settings.get("clawdmeter"):
            link.stop()
            for _ in range(30):
                if not link.is_running():
                    break
                time.sleep(0.1)
            link.start()
        return self.clawdmeter_state()

    def clawdmeter_set(self, on):
        """Anbindung ein-/ausschalten."""
        on = bool(on)
        self.settings["clawdmeter"] = on
        save_json(SETTINGS_FILE, self.settings)
        link = self._clawd_link()
        if link is not None:
            link.start() if on else link.stop()
        return self.clawdmeter_state()

    def set_autostart(self, on):
        """Autostart im Windows-Registry setzen und Setting speichern."""
        ok = set_autostart(bool(on))
        self.settings["autostart"] = bool(on)
        save_json(SETTINGS_FILE, self.settings)
        return {"ok": ok, "enabled": bool(on)}

    def buddy_apply_tray(self, on):
        """Tray-Icon starten/stoppen wenn Toggle sich aendert."""
        tray = getattr(self, "_tray", None)
        if not tray:
            return {"ok": False}
        try:
            if on:
                tray.start()
            else:
                tray.stop()
        except Exception:
            pass
        return {"ok": True}

    def buddy_real_quit(self):
        """App wirklich beenden (statt in Tray verstecken)."""
        fn = getattr(self, "_real_quit", None)
        if fn:
            try:
                fn()
            except Exception:
                pass
        return {"ok": True}

    def buddy_notify_view(self, view):
        """Wird beim Tab-Wechsel im UI aufgerufen. Speichert die aktuelle
        Ansicht im Api-Objekt (nicht persistiert) – die Buddy-Loop nutzt es,
        um den Buddy im Buddy-Tab sichtbar zu lassen, sonst zu verstecken
        waehrend der Session Browser vorne ist."""
        self._current_view = str(view or "sessions")
        return {"ok": True}

    def buddy_preview_anim(self, name):
        """Zeigt eine bestimmte Animation kurz auf dem Buddy."""
        if not self.buddy.is_alive():
            # Falls Buddy aus: kurz anwerfen, ist nicht schlimm
            bud = self.settings.setdefault("buddy", dict(DEFAULT_SETTINGS["buddy"]))
            bud["enabled"] = True
            save_json(SETTINGS_FILE, self.settings)
            self.buddy.start()
            time.sleep(0.3)
        self.buddy.preview_anim(name, 3.5)
        return {"ok": True}

    def buddy_monitors(self):
        """Alle Monitore mit Label/Groesse fuer den Picker."""
        return _win_enum_monitors()

    def buddy_anchor(self, anchor, monitor_idx=None):
        """Springt zu einem benannten Ankerpunkt (tl,tc,tr,ml,c,mr,bl,bc,br)
        auf einem bestimmten Monitor (oder dem aktuellen wenn None)."""
        bud = self.settings.setdefault("buddy", dict(DEFAULT_SETTINGS["buddy"]))
        if not bud.get("enabled"):
            bud["enabled"] = True
            save_json(SETTINGS_FILE, self.settings)
            self.buddy.start()
            time.sleep(0.4)
        scale = max(2, min(10, int(bud.get("size", 4))))
        # Rahmen berücksichtigen – das Fenster ist ggf. groesser als 20*scale.
        pad = _frame_pad(_resolved_frame_style(bud), scale)
        size_px = 20 * scale + pad["l"] + pad["r"]
        mi = int(monitor_idx) if monitor_idx is not None else None
        nx, ny = _anchor_position(anchor, size_px, int(bud.get("x", 200)),
                                  int(bud.get("y", 200)), mi)
        self.buddy.jump_to(nx, ny)
        # Optimistisch schon merken (der Buddy-Thread persistiert nochmal
        # nach dem Snap – kann leicht abweichen, dann gewinnt der Thread).
        bud["x"], bud["y"] = nx, ny
        save_json(SETTINGS_FILE, self.settings)
        return self.buddy_state()

    def buddy_place(self):
        """Positionier-Modus: Hauptfenster minimieren, Buddy pulsieren lassen,
        auf ersten Drop warten. Danach Hauptfenster wieder holen."""
        bud = self.settings.setdefault("buddy", dict(DEFAULT_SETTINGS["buddy"]))
        was_off = not bud.get("enabled")
        if was_off:
            bud["enabled"] = True
            save_json(SETTINGS_FILE, self.settings)
            self.buddy.start()
            # kurze Wartezeit bis der Tkinter-Thread hochgefahren ist
            for _ in range(30):
                if self.buddy.is_alive():
                    break
                time.sleep(0.1)

        # Fenster bleibt offen – das Grid-Overlay legt sich davor.
        self.buddy.place_mode(on_done=None)
        return {"ok": True}

    def buddy_preview(self, name):
        """Liefert einen Vorschau-Frame als PNG-Data-URL."""
        return self._buddy_preview_gif(name)

    def buddy_icon(self, name):
        """Wie buddy_preview, aber auf die Figur zugeschnitten und mit
        durchsichtigem Rand - fuer kleine Stellen wie die Ueberschrift.

        Die Sprites sind 20x20, die Figur belegt davon nur etwa 15x13; der
        Rest ist schwarze Flaeche. Ungeschnitten in ein 32-px-Kaestchen
        gesetzt ergibt das einen kleinen Klecks in einem schwarzen Quadrat,
        der wegen des Seitenverhaeltnisses gedrueckt aussieht.
        """
        return _sprite_icon_png(BUDDY_ANIMS.get(name))

    def _buddy_preview_gif(self, name):
        """Baut aus dem ersten Frame einer Animation ein 80x80 PNG-Data-URL.
        (Fuer die Auswahl-Liste im Buddy-Tab.)"""
        anim = BUDDY_ANIMS.get(name)
        if not anim:
            return ""
        frame = anim["frames"][0]
        palette = anim["palette"]
        scale = 4
        # PNG selbst bauen ist Overkill – wir generieren stattdessen ein
        # BMP mit 4x-Scale und liefern es als data-uri.
        w = 20 * scale
        h = 20 * scale
        # 24-bit BMP, unten-nach-oben.
        row_bytes = w * 3
        pad = (4 - row_bytes % 4) % 4
        pixels = bytearray()
        for row in range(19, -1, -1):
            for _ in range(scale):
                for col in range(20):
                    idx = frame[row * 20 + col]
                    if idx <= 0:
                        r, g, b = 20, 16, 14   # dunkler App-Hintergrund
                    else:
                        hx = palette[idx] if idx < len(palette) else "#000000"
                        hx = hx.lstrip("#")
                        r = int(hx[0:2], 16); g = int(hx[2:4], 16); b = int(hx[4:6], 16)
                    for _ in range(scale):
                        pixels += bytes((b, g, r))
                pixels += bytes(pad)
        file_size = 54 + len(pixels)
        header = bytearray()
        header += b"BM"
        header += file_size.to_bytes(4, "little")
        header += b"\x00\x00\x00\x00"
        header += (54).to_bytes(4, "little")
        header += (40).to_bytes(4, "little")
        header += w.to_bytes(4, "little", signed=True)
        header += h.to_bytes(4, "little", signed=True)
        header += (1).to_bytes(2, "little")
        header += (24).to_bytes(2, "little")
        header += (0).to_bytes(4, "little")
        header += len(pixels).to_bytes(4, "little")
        header += (2835).to_bytes(4, "little")
        header += (2835).to_bytes(4, "little")
        header += (0).to_bytes(4, "little")
        header += (0).to_bytes(4, "little")
        raw = bytes(header) + bytes(pixels)
        return "data:image/bmp;base64," + base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _ssl_ctx():
        # Echte TLS-Verifizierung (der Updater laedt eine ausfuehrbare Datei, daher
        # darf TLS nicht abgeschaltet werden). Bevorzugt den Windows-Zertifikat-
        # speicher (funktioniert auch hinter TLS-Inspektion/Firewalls), sonst certifi.
        try:
            import truststore
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            pass
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def _remote_info(self, timeout=4):
        req = urllib.request.Request(
            UPDATE_URL, headers={"User-Agent": "ClaudeSessionBrowser"})
        with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx()) as r:
            return json.loads(r.read().decode("utf-8"))

    def consume_update_failed_marker(self):
        """Prueft ob der letzte Update-Batch fehlgeschlagen ist (Datei-Move
        klappte nicht). Loescht den Marker und meldet True, damit die UI
        einen Toast zeigen kann."""
        marker = os.path.join(tempfile.gettempdir(),
                              "csb_update_failed.marker")
        if os.path.isfile(marker):
            try:
                os.remove(marker)
            except OSError:
                pass
            return True
        return False

    def check_update(self):
        """Fragt bei GitHub nach einer neueren Version. Unterscheidet zwischen
        Netzwerk-Fehler und "wirklich aktuell" damit die UI unterscheiden kann."""
        frozen = bool(getattr(sys, "frozen", False))
        try:
            data = self._remote_info()
            self._update_info = data
            latest = data.get("version", "0")
            avail = _vtuple(latest) > _vtuple(VERSION)
            return {"available": avail, "latest": latest, "current": VERSION,
                    "url": data.get("url", ""), "notes": data.get("notes", ""),
                    "frozen": frozen, "error": ""}
        except Exception as e:
            # Explizit Fehler-Info liefern, damit die UI "Netzwerkfehler"
            # von "aktuelle Version" trennen kann.
            return {"available": False, "current": VERSION, "frozen": frozen,
                    "error": type(e).__name__ + ": " + str(e)[:120]}

    def _install_via_installer(self, data, installer_url):
        """Installer-basiertes Update (v1.1.0+). Laedt Setup.exe, prueft
        SHA-256, startet Silent-Install, danach neue App vom Standard-
        Install-Pfad. Alter Runner-Pfad wird nicht angefasst -- er bleibt
        als Orphan liegen (harmlos), User kann ihn manuell loeschen."""
        if getattr(self, "_installing", False):
            return {"ok": False, "error": t("Update läuft bereits.")}
        self._installing = True
        part = os.path.join(tempfile.gettempdir(),
                            "ClaudeSessionBrowser_setup.exe.part")
        win = self._win()

        def js(code):
            if win:
                try:
                    win.evaluate_js(code)
                except Exception:
                    pass

        try:
            import time
            setup = os.path.join(tempfile.gettempdir(),
                                 "ClaudeSessionBrowser_setup.exe")
            req = urllib.request.Request(
                installer_url, headers={"User-Agent": "ClaudeSessionBrowser"})
            with urllib.request.urlopen(
                    req, timeout=120, context=self._ssl_ctx()) as r, \
                    open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        p = int(done * 100 / total)
                        if p != last:
                            last = p
                            js("window.updateProgress&&updateProgress(%d)" % p)
            size = os.path.getsize(part)
            if (total and size != total) or size < 500_000:
                try: os.remove(part)
                except OSError: pass
                return {"ok": False,
                        "error": t("Installer-Download unvollständig.")}
            with open(part, "rb") as f:
                if f.read(2) != b"MZ":
                    os.remove(part)
                    return {"ok": False,
                            "error": t("Heruntergeladener Installer ist keine gültige .exe.")}
            # SHA-256 Pruefung (installer_sha256 bevorzugt, Fallback sha256)
            import hashlib
            expected = str(data.get("installer_sha256")
                           or data.get("sha256") or "").strip().lower()
            if expected:
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    try: os.remove(part)
                    except OSError: pass
                    return {"ok": False,
                            "error": t("Ungültiger SHA-256 im Server-Manifest.")}
                h = hashlib.sha256()
                with open(part, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                if h.hexdigest() != expected:
                    try: os.remove(part)
                    except OSError: pass
                    return {"ok": False,
                            "error": t("Integritäts-Prüfung fehlgeschlagen "
                                       "(SHA-256). Update abgebrochen.")}
            if os.path.exists(setup):
                try: os.remove(setup)
                except OSError: pass
            os.replace(part, setup)

            # Updater-Pfad: neben der aktuellen exe oder im Install-Ordner
            lad = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            install_dir = os.path.join(lad, "Programs", "ClaudeSessionBrowser")

            # Updater suchen: erst neben aktueller exe, dann im Install-Ordner
            updater = None
            if getattr(sys, "frozen", False):
                updater = os.path.join(os.path.dirname(sys.executable),
                                       "csb_updater.exe")
            if not updater or not os.path.exists(updater):
                updater = os.path.join(install_dir, "csb_updater.exe")

            js("window.downloadDone&&downloadDone()")

            if os.path.exists(updater):
                # Neuer Weg: Updater starten, der macht alles
                time.sleep(0.5)
                DETACHED = 0x00000008 | 0x00000200
                subprocess.Popen([updater, "--install", setup],
                                 creationflags=DETACHED, close_fds=True)
                time.sleep(0.5)
                if win:
                    win.destroy()
            else:
                # Fallback: Batch (fuer alte Installationen ohne Updater)
                bat = os.path.join(tempfile.gettempdir(), "csb_installer.bat")
                with open(bat, "w", encoding="utf-8") as f:
                    f.write(
                        "@echo off\r\n"
                        'set "SETUP=' + setup + '"\r\n'
                        "ping -n 6 127.0.0.1 >nul\r\n"
                        '"%SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL\r\n'
                        'del "%SETUP%" >nul 2>&1\r\n'
                        'del "%~f0"\r\n'
                    )
                time.sleep(2.6)
                NOWIN = 0x08000000 | 0x00000200
                subprocess.Popen(["cmd", "/c", bat], creationflags=NOWIN)
                if win:
                    win.destroy()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            self._installing = False
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass

    def install_update(self):
        """Laedt die neue Version und aktualisiert. Zwei Wege:
        1) Wenn version.json ein installer_url-Feld hat: Installer laden + im
           Silent-Mode ausfuehren (v1.1.0+, sauberer Ansatz).
        2) Sonst: alte Onefile-Replace-Logik (Rueckwaerts-Kompat fuer 1.0.x
           Server-Manifeste falls die ohne installer_url veroeffentlicht sind).
        Einstellungen/Daten in ~/.claude bleiben in beiden Faellen unberuehrt."""
        try:
            data = getattr(self, "_update_info", None) or self._remote_info()
        except Exception:
            return {"ok": False, "error": t("Kein Internet / Repo nicht erreichbar.")}
        page = data.get("url") or \
            "https://github.com/juppeee/claude-session-browser/releases/latest"
        installer_url = data.get("installer_url") or ""
        exe_url = data.get("exe_url") or ""

        # Im Entwicklungsmodus (.py, keine .exe): nur Release-Seite oeffnen
        if not getattr(sys, "frozen", False):
            webbrowser.open(page)
            return {"ok": False, "reason": "dev", "opened": True}

        # Weg 1: Installer-Update
        if installer_url:
            return self._install_via_installer(data, installer_url)

        # Weg 2: Onefile-Replace (Legacy)
        if not exe_url:
            webbrowser.open(page)
            return {"ok": False, "reason": "no_exe_url", "opened": True}

        if getattr(self, "_installing", False):
            return {"ok": False, "error": t("Update läuft bereits.")}
        self._installing = True

        win = self._win()

        def js(code):
            if win:
                try:
                    win.evaluate_js(code)
                except Exception:
                    pass

        part = os.path.join(tempfile.gettempdir(), "ClaudeSessionBrowser_update.exe.part")
        try:
            import time
            cur = sys.executable
            target_dir = os.path.dirname(cur) or "."
            # Download zuerst in eine .part-Datei in einem IMMER beschreibbaren Temp-
            # Ordner; erst nach vollstaendiger Pruefung in die finale .new umbenennen.
            new = os.path.join(tempfile.gettempdir(), "ClaudeSessionBrowser_update.exe")
            part = new + ".part"
            req = urllib.request.Request(
                exe_url, headers={"User-Agent": "ClaudeSessionBrowser"})
            with urllib.request.urlopen(req, timeout=120, context=self._ssl_ctx()) as r, \
                    open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                last = -1
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        p = int(done * 100 / total)
                        if p != last:
                            last = p
                            js("window.updateProgress&&updateProgress(%d)" % p)

            # Vollstaendigkeit pruefen: heruntergeladene Groesse muss exakt passen,
            # sonst wuerde eine kaputte .exe getauscht -> "Failed to load Python DLL".
            size = os.path.getsize(part)
            if (total and size != total) or size < 2_000_000:
                try:
                    os.remove(part)
                except OSError:
                    pass
                return {"ok": False,
                        "error": t("Download unvollständig – bitte erneut versuchen.")}
            # MZ-Header pruefen (gueltige .exe?)
            with open(part, "rb") as f:
                if f.read(2) != b"MZ":
                    os.remove(part)
                    return {"ok": False, "error": t("Heruntergeladene Datei ist keine gültige .exe.")}

            # Integritaets-Pruefung ueber SHA-256, wenn im version.json angegeben.
            # Feld ist optional (aeltere version.json ohne sha256 laufen ohne Check
            # durch – Backward-Compat) aber wenn angegeben MUSS er passen.
            import hashlib
            expected = str(data.get("sha256") or "").strip().lower()
            if expected:
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    try: os.remove(part)
                    except OSError: pass
                    return {"ok": False,
                            "error": t("Ungültiger SHA-256 im Server-Manifest.")}
                h = hashlib.sha256()
                with open(part, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                actual = h.hexdigest()
                if actual != expected:
                    try: os.remove(part)
                    except OSError: pass
                    return {"ok": False,
                            "error": t("Integritäts-Prüfung fehlgeschlagen "
                                       "(SHA-256 stimmt nicht). Update abgebrochen.")}

            if os.path.exists(new):
                os.remove(new)
            os.replace(part, new)   # atomar

            # Ist der Zielordner beschreibbar? (C:\ etc. brauchen Admin)
            writable = True
            try:
                _t = os.path.join(target_dir, ".csb_write_test")
                with open(_t, "w") as _f:
                    _f.write("x")
                os.remove(_t)
            except OSError:
                writable = False

            # Batch: wartet bis die laufende .exe frei ist, tauscht aus, startet neu.
            # Laeuft komplett unsichtbar (CREATE_NO_WINDOW) -> kein Ping-Fenster.
            # Bricht nach ~60 Versuchen ab und startet die App trotzdem wieder
            # (kein Endlos-Geist-Prozess). Relaunch ueber explorer.exe -> laeuft
            # als normaler Nutzer (auch wenn der Tausch elevated lief).
            bat = os.path.join(tempfile.gettempdir(), "csb_update.bat")
            marker = os.path.join(tempfile.gettempdir(),
                                  "csb_update_failed.marker")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(
                    "@echo off\r\n"
                    'set "CUR=' + cur + '"\r\n'
                    'set "NEW=' + new + '"\r\n'
                    'set "FAIL=' + marker + '"\r\n'
                    'if exist "%FAIL%" del "%FAIL%"\r\n'
                    "set /a n=0\r\n"
                    ":wait\r\n"
                    "ping -n 2 127.0.0.1 >nul\r\n"
                    'move /y "%NEW%" "%CUR%" >nul 2>&1\r\n'
                    'if not exist "%NEW%" goto done\r\n'
                    "set /a n+=1\r\n"
                    "if %n% lss 60 goto wait\r\n"
                    "rem Move gescheitert – Fehler-Marker schreiben\r\n"
                    'echo swap failed > "%FAIL%"\r\n'
                    ":done\r\n"
                    "ping -n 2 127.0.0.1 >nul\r\n"
                    'explorer.exe "%CUR%"\r\n'
                    'del "%~f0"\r\n'
                )

            js("window.downloadDone&&downloadDone()")
            time.sleep(2.6)        # die "Bereit!"-Animation abspielen lassen

            NOWIN = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            if writable:
                subprocess.Popen(["cmd", "/c", bat], creationflags=NOWIN)
            else:
                # Geschuetzter Ort -> Tausch mit Adminrechten (einmal UAC), unsichtbar
                ps = ("Start-Process -FilePath cmd.exe "
                      "-ArgumentList '/c','\"%s\"' -Verb RunAs -WindowStyle Hidden" % bat)
                subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                                  "-Command", ps], creationflags=NOWIN)
            if win:
                win.destroy()      # entsperrt die .exe -> Batch tauscht & startet neu
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            self._installing = False
            try:
                if os.path.exists(part):
                    os.remove(part)   # angefangenen Download aufraeumen
            except OSError:
                pass

    def open_url(self, url):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def toggle_fullscreen(self):
        win = self._win()
        if win:
            win.toggle_fullscreen()

    def minimize(self):
        win = self._win()
        if win:
            win.minimize()

    def close(self):
        win = self._win()
        if win:
            win.destroy()


# --------------------------------------------------------------------------- #
#  HTML / CSS / JS
# --------------------------------------------------------------------------- #
def logo_data_uri():
    try:
        with open(_resource("logo.png"), "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return ""


def build_html():
    return (HTML_TEMPLATE
            .replace("__LOGO__", logo_data_uri())
            .replace("__I18N__", i18n.js_payload()))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --bg:#14100e; --surface:#1f1814; --surface2:#2b211b; --row:#191412;
    --row-alt:#1e1814; --border:#2d231d; --fg:#f3ece7; --muted:#9a8c83;
    --accent:#ec7456; --accent2:#f5926f; --select:#4a3327;
  }
  *{box-sizing:border-box; margin:0; padding:0}
  html,body{height:100%}
  body{
    font-family:"Segoe UI",system-ui,sans-serif; color:var(--fg);
    background:var(--bg); overflow:hidden; user-select:none;
    font-size:14px; color-scheme:dark;   /* native Steuerelemente (Dropdown etc.) dunkel */
  }
  .app{display:flex; flex-direction:column; height:100vh}

  /* ---- Titelleiste ---- */
  .titlebar{
    height:44px; display:flex; align-items:center; gap:10px; padding:0 6px 0 12px;
    background:linear-gradient(90deg,#1a130f,#1f1714);
    border-bottom:1px solid var(--border); flex:none;
  }
  .titlebar .logo{width:26px; height:26px; flex:none}
  .l-spin{transform-origin:512px 512px; animation:l-spin 28s linear infinite}
  .l-pulse{transform-origin:512px 512px; animation:l-pulse 4s ease-in-out infinite}
  @keyframes l-spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
  @keyframes l-pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.04)}}
  .titlewrap{display:flex; align-items:center; gap:13px}
  .hlogo{width:34px; height:34px; flex:none}
  .titlebar .tt{font-weight:600; font-size:13px; color:var(--muted); letter-spacing:.3px}
  .drag{flex:1; height:100%}
  .winbtns{display:flex; gap:2px}
  .winbtn{
    width:42px; height:30px; border:none; background:transparent; color:var(--muted);
    border-radius:8px; cursor:pointer; font-size:14px; display:grid; place-items:center;
  }
  .winbtn:hover{background:var(--surface2); color:var(--fg)}
  .winbtn.close:hover{background:#e54b58; color:#fff}

  /* ---- Tabs ---- */
  .tabs{display:flex; gap:4px; padding:10px 18px 0; background:var(--bg); flex:none}
  .tab{
    padding:9px 18px; font-weight:600; color:var(--muted); cursor:pointer;
    border-radius:9px 9px 0 0; position:relative; font-size:13.5px;
  }
  .tab:hover{color:var(--fg)}
  .tab.active{color:var(--fg)}
  .tab.active::after{
    content:""; position:absolute; left:14px; right:14px; bottom:-1px; height:2.5px;
    background:var(--accent); border-radius:3px;
  }

  .updatebar{display:none; align-items:center; gap:11px; margin:4px 18px 0; padding:10px 14px;
    background:color-mix(in srgb, var(--accent) 13%, transparent);
    border:1px solid var(--accent); border-radius:11px; color:var(--accent2)}
  .updatebar.show{display:flex}
  .updatebar .utext{font-weight:700; color:var(--fg)}
  .updatebar .unotes{color:var(--muted); font-size:12.5px; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  #upd-notes{color:var(--fg); font-size:13.5px; line-height:1.65; margin-bottom:12px;
    max-height:260px; overflow:auto; white-space:pre-wrap; background:var(--bg);
    border:1px solid var(--border); border-radius:10px; padding:12px 14px}
  .upd-keep{color:var(--muted); font-size:12px; margin-bottom:16px; display:flex; gap:7px; align-items:center}

  /* ---- Update-Animation ---- */
  #upd-progress{display:none; text-align:center; padding:4px 4px 6px}
  #upd-pop.installing #upd-info{display:none}
  #upd-pop.installing #upd-progress{display:block}
  .inst-stage{height:128px; display:grid; place-items:center; position:relative}
  .inst-logo{width:104px; height:104px; animation:l-spin 2.6s linear infinite;
    transition:opacity .35s, transform .45s}
  .inst-check{width:104px; height:104px; position:absolute; opacity:0; transform:scale(.4)}
  .inst-check circle{fill:none; stroke:var(--accent2); stroke-width:3;
    stroke-dasharray:145; stroke-dashoffset:145}
  .inst-check path{fill:none; stroke:#fff; stroke-width:4.5; stroke-linecap:round;
    stroke-linejoin:round; stroke-dasharray:40; stroke-dashoffset:40}
  #upd-pop.ready .inst-logo{opacity:0; transform:scale(.3)}
  #upd-pop.ready .inst-check{opacity:1; transform:scale(1);
    transition:opacity .3s, transform .55s cubic-bezier(.2,1.5,.4,1)}
  #upd-pop.ready .inst-check circle{animation:draw-c .5s ease forwards}
  #upd-pop.ready .inst-check path{animation:draw-p .4s .35s ease forwards}
  @keyframes draw-c{to{stroke-dashoffset:0}}
  @keyframes draw-p{to{stroke-dashoffset:0}}

  .bar{height:12px; background:var(--bg); border:1px solid var(--border); border-radius:20px;
    overflow:hidden; margin:16px 0 9px; position:relative}
  .bar-fill{height:100%; width:0%; border-radius:20px;
    background:linear-gradient(90deg,var(--accent),var(--accent2));
    box-shadow:0 0 14px var(--accent); transition:width .25s ease}
  .bar-shine{position:absolute; inset:0; border-radius:20px;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,.4),transparent);
    background-size:40% 100%; background-repeat:no-repeat; animation:shine 1.1s linear infinite}
  #upd-pop.ready .bar-shine{display:none}
  @keyframes shine{from{background-position:-45% 0}to{background-position:145% 0}}
  .inst-state{font-weight:700; font-size:15.5px; margin-top:6px}
  #upd-pop.ready .inst-state{color:var(--accent2)}
  .inst-pct{color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; margin-top:3px}
  #upd-pop.ready .inst-pct{opacity:0}

  .confetti{position:absolute; inset:0; pointer-events:none; overflow:visible}
  .confetti i{position:absolute; left:50%; top:46%; width:9px; height:9px; border-radius:2px; opacity:0}
  #upd-pop.ready .confetti i{animation:cfetti .95s ease-out forwards}
  @keyframes cfetti{0%{opacity:1; transform:translate(-50%,-50%) scale(1) rotate(0)}
    100%{opacity:0; transform:translate(calc(-50% + var(--dx)),calc(-50% + var(--dy))) scale(.3) rotate(220deg)}}

  .view{flex:1; overflow:hidden; display:none; flex-direction:column; padding:14px 18px 16px}
  .view.active{display:flex}

  /* ---- Kopf ---- */
  .head{display:flex; align-items:baseline; justify-content:space-between; margin:6px 2px 12px}
  .head h1{font-size:26px; font-weight:700; letter-spacing:-.5px}
  .head h1 .g{
    background:linear-gradient(90deg,var(--accent),var(--accent2));
    -webkit-background-clip:text; background-clip:text; color:transparent;
  }
  .count{color:var(--muted); font-size:13px}

  /* ---- Suchzeile ---- */
  .searchbar{display:flex; gap:10px; margin-bottom:12px}
  .search{
    flex:1; display:flex; align-items:center; gap:9px; background:var(--surface);
    border:1px solid var(--border); border-radius:12px; padding:0 14px; height:42px;
    transition:border-color .15s, box-shadow .15s;
  }
  .search:focus-within{border-color:var(--accent);
    box-shadow:0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent)}
  .search svg{flex:none; color:var(--muted)}
  .search input{
    flex:1; background:transparent; border:none; outline:none; color:var(--fg);
    font-size:14px; font-family:inherit;
  }
  .btn{
    display:inline-flex; align-items:center; gap:8px; height:42px; padding:0 16px;
    border:none; border-radius:11px; background:var(--surface); color:var(--fg);
    font-family:inherit; font-size:13.5px; font-weight:600; cursor:pointer;
    border:1px solid var(--border); transition:background .13s, transform .05s;
  }
  .btn:hover{background:var(--surface2)}
  .btn:active{transform:translateY(1px)}
  .btn[disabled]{opacity:.4; cursor:default; pointer-events:none}
  .btn.accent{background:var(--accent); border-color:transparent; color:#fff}
  .btn.accent:hover{background:var(--accent2)}
  .btn svg{flex:none}
  .btn.mini{height:30px; padding:0 10px; font-size:12px; border-radius:8px}
  .cell.mono{font-family:Consolas,monospace; font-size:12px}

  /* ---- Tabelle ---- */
  .table{
    flex:1; min-height:140px; display:flex; flex-direction:column; background:var(--row);
    border:1px solid var(--border); border-radius:14px; overflow:hidden;
  }
  .thead{
    display:grid; grid-template-columns:var(--cols); gap:0; padding:0 6px;
    background:var(--bg); border-bottom:1px solid var(--border); flex:none;
  }
  .th{
    padding:13px 12px; font-size:11.5px; font-weight:700; color:var(--muted);
    text-transform:uppercase; letter-spacing:.6px; cursor:pointer; white-space:nowrap;
    display:flex; align-items:center; gap:5px;
  }
  .th:hover{color:var(--fg)}
  .th.num{justify-content:flex-start}
  .th .arr{font-size:10px; opacity:.9}
  .tbody{flex:1; overflow-y:auto; padding:5px}
  .row{
    display:grid; grid-template-columns:var(--cols); align-items:center;
    padding:0 6px; border-radius:10px; cursor:default; position:relative;
    transition:background .1s;
  }
  .row .cell{padding:11px 12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .row .title{font-weight:600}
  .row .dim{color:var(--muted); font-size:13px}
  .row .ic{display:inline-flex; align-items:center; gap:7px}
  .row .ic svg{flex:none; opacity:.65}
  .row:nth-child(even){background:var(--row-alt)}
  .row:hover{background:var(--surface)}
  /* Auswahl: heller Ring + schwebender Schatten -> hebt sich auf jeder Zeilenfarbe ab */
  .row.sel{background:var(--select); z-index:2;
    box-shadow:inset 0 0 0 2px rgba(255,255,255,.92), 0 6px 20px rgba(0,0,0,.55)}
  .row.sel .title{font-weight:700}
  .row.colored{margin:1px 0}
  .row.colored .dim{color:inherit; opacity:.85}
  .row.colored .ic svg{opacity:.8}

  .empty{flex:1; display:grid; place-items:center; color:var(--muted); text-align:center; padding:30px}
  .empty .big{font-size:15px; color:var(--fg); margin-bottom:8px; font-weight:600}

  /* ---- Hauptbereich: Tabelle links, Panel rechts (nur bei Auswahl) ---- */
  .main{flex:1; display:flex; gap:14px; min-height:0}
  .side{width:320px; flex:none; display:flex; flex-direction:column; gap:12px; min-height:0}
  .main:not(.show-side) .side{display:none}
  .main.show-side .side{animation:slidein .22s ease}
  @keyframes slidein{from{opacity:0; transform:translateX(22px)} to{opacity:1; transform:none}}

  /* ---- Detail + Aktionen (rechtes Panel) ---- */
  /* Frueher ein einziges Monospace-Feld mit pre-wrap: die ID brach ueber zwei
     Zeilen um, die erste Frage stand als Textklotz darunter, und alles hatte
     dasselbe Gewicht. Jetzt echte Zeilen mit Beschriftung, Fliesstext im
     normalen Schnitt und die ID klein am Fuss - sie ist selten interessant,
     nahm aber den meisten Platz. */
  .detail{
    flex:1; min-height:90px; background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:14px 15px 12px; font-size:12.5px;
    color:var(--muted); overflow:auto; line-height:1.5; user-select:text;
    display:flex; flex-direction:column; gap:11px;
  }
  .detail .dt-empty{margin:auto; text-align:center; padding:0 10px}
  .dt-head{color:var(--fg); font-size:14px; font-weight:700; line-height:1.35;
    word-break:break-word}
  .dt-rows{display:grid; grid-template-columns:auto minmax(0,1fr); gap:5px 12px}
  .dt-rows .k{color:var(--muted); font-size:11.5px; letter-spacing:.02em;
    padding-top:1px}
  .dt-rows .v{color:var(--fg); overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap}
  .dt-rows .v.path{font-family:Consolas,monospace; font-size:12px}
  /* Die Zeilenbegrenzung sitzt im inneren Element, nicht auf dem Flex-Kind:
     -webkit-box als direktes Flex-Kind streckt sich, dann laeuft die
     Randlinie durch das halbe Panel. */
  .dt-quote{border-left:2px solid var(--border); padding-left:10px; flex:none}
  .dt-quote .k{display:block; color:var(--muted); font-size:11.5px; margin-bottom:3px}
  .dt-quote .t{
    color:var(--fg); line-height:1.55; display:-webkit-box; -webkit-line-clamp:6;
    -webkit-box-orient:vertical; overflow:hidden;
  }
  /* ID als normale Zeile, gekuerzt. Ein eigener Kopieren-Knopf waere doppelt:
     direkt darunter steht schon der Knopf "ID" in der Aktionsreihe. */
  .dt-rows .v.id{font-family:Consolas,monospace; font-size:11.5px; color:var(--muted)}
  .actions{display:flex; flex-direction:column; gap:9px; flex:none}
  .actions .btn{width:100%; justify-content:center}
  .actions .hint{color:var(--muted); font-size:12px; text-align:center; margin-top:2px}
  /* Zweitrangige Aktionen nebeneinander statt gestapelt. */
  .iconrow{display:flex; gap:6px}
  .iconbtn{flex:1; display:flex; flex-direction:column; align-items:center; gap:4px;
    background:var(--surface2); border:1px solid var(--border); color:var(--fg);
    border-radius:10px; padding:9px 4px 7px; cursor:pointer; font-family:inherit;
    font-size:11px; letter-spacing:.02em; transition:border-color .12s, background .12s}
  .iconbtn svg{width:17px; height:17px}
  .iconbtn:hover:not(:disabled){border-color:var(--accent); background:var(--bg)}
  .iconbtn:disabled{opacity:.4; cursor:default}

  /* Fusszeile mit den Tastaturkuerzeln - auf jedem Tab, mit dem jeweils
     passenden Inhalt. Steht ausserhalb der Ansichten, damit sie beim
     Wechseln nicht springt. */
  .shortcutbar{display:flex; gap:16px; flex-wrap:wrap; align-items:center;
    padding:8px 18px 10px; border-top:1px solid var(--border);
    color:var(--muted); font-size:11.5px}
  .shortcutbar b{color:var(--fg); font-weight:600}
  .shortcutbar kbd{background:var(--surface2); border:1px solid var(--border);
    border-bottom-width:2px; border-radius:5px; padding:1px 5px; margin-right:5px;
    font-family:Consolas,monospace; font-size:10.5px; color:var(--fg)}

  /* ---- Einstellungen ---- */
  .settings{overflow-y:auto; flex:1; padding-right:6px}
  .card{
    background:var(--surface); border:1px solid var(--border); border-radius:14px;
    padding:18px 20px; margin-bottom:14px;
  }
  .card h2{font-size:15px; margin-bottom:4px; display:flex; align-items:center; gap:10px}
  /* Icon-Chip in der Kartenueberschrift. Gibt jeder Karte eine eigene
     Silhouette - vorher waren zwoelf identische Boxen untereinander und man
     musste jede Ueberschrift lesen um sich zu orientieren. */
  .card h2 .ci{
    flex:0 0 auto; width:28px; height:28px; border-radius:9px;
    display:grid; place-items:center;
    background:var(--bg); border:1px solid var(--border); color:var(--accent);
  }
  .card h2 .ci svg{width:16px; height:16px; display:block}
  /* Sektionsband: bricht die Kartenkette in benannte Gruppen. */
  .secthead{
    font-size:11.5px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:var(--muted); margin:22px 2px 10px; display:flex; align-items:center; gap:10px;
  }
  .secthead:first-child{margin-top:2px}
  /* Sprungleiste: die Einstellungen sind auf fuenf Gruppen und weit ueber
     eine Bildschirmhoehe angewachsen. scroll-margin-top sorgt dafuer, dass
     die angesprungene Ueberschrift nicht am oberen Rand klebt. */
  .secthead{scroll-margin-top:8px}
  .jumpbar{display:flex; gap:6px; flex-wrap:wrap; margin:0 0 12px; padding-bottom:11px;
    border-bottom:1px solid var(--border)}
  .jumpbar button{background:transparent; border:1px solid transparent; color:var(--muted);
    font-family:inherit; font-size:12px; font-weight:600; letter-spacing:.03em;
    padding:5px 11px; border-radius:8px; cursor:pointer; transition:color .12s, background .12s}
  .jumpbar button:hover{color:var(--fg); background:var(--surface2)}
  .jumpbar button.active{color:var(--fg); background:var(--surface2);
    border-color:color-mix(in srgb, var(--accent) 45%, transparent)}
  .secthead::after{content:""; flex:1; height:1px; background:var(--border)}
  .card .sub{color:var(--muted); font-size:13px; margin-bottom:14px}
  /* alle Eingabefelder in Karten dunkel (kein weisses Standard-Feld) */
  .card input[type=text], .card input[type=number]{
    background:var(--bg); border:1px solid var(--border); color:var(--fg);
    border-radius:9px; padding:9px 12px; font-family:inherit; font-size:13.5px; outline:none}
  .card input[type=text]:focus, .card input[type=number]:focus{border-color:var(--accent)}
  .field{display:flex; gap:10px; align-items:center; flex-wrap:wrap}
  .field input[type=text]{
    flex:1; min-width:240px; background:var(--bg); border:1px solid var(--border);
    color:var(--fg); border-radius:10px; padding:11px 13px; font-family:inherit; font-size:13.5px;
    outline:none;
  }
  .field input[type=text]:focus{border-color:var(--accent)}
  .badge{padding:4px 11px; border-radius:20px; font-size:12px; font-weight:600}
  .badge.ok{background:rgba(62,207,142,.16); color:#5fe0a6}
  .badge.no{background:rgba(229,75,88,.16); color:#ff8088}
  .row2{display:flex; align-items:center; justify-content:space-between; gap:14px; padding:9px 0}
  .row2 + .row2{border-top:1px solid var(--border)}
  .row2 .lbl{font-weight:600}
  .row2 .desc{color:var(--muted); font-size:12.5px; margin-top:2px}
  /* Folgenhinweise. Beschreibungen duerfen im gedaempften Grau stehen, aber
     ein Satz ueber eine Folge, die man nicht mehr zurueckdrehen kann, geht
     darin unter - der bekommt Farbe und ein Zeichen davor. */
  /* Aktueller Stand des 5-Stunden-Limits, ueber den Schaltern zu denen er
     gehoert. Faerbt sich mit der Lage - im vollen Limit soll man nicht erst
     lesen muessen. */
  /* Zwei Kacheln nach dem Vorbild des Clawdmeter: grosse Zahl, Balken,
     Restzeit. Eine Zeile ueber die volle Breite war fast nur Leerraum. */
  .limitbox{display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr));
    gap:10px; margin:0}
  .lcard{background:var(--bg); border:1px solid var(--border); border-radius:9px;
    padding:8px 11px 9px}
  .lcard .top{display:flex; align-items:baseline; gap:8px}
  .lcard .num{font-size:17px; font-weight:700; color:var(--fg); line-height:1;
    font-variant-numeric:tabular-nums}
  .lcard .tag{margin-left:auto; font-size:10.5px; color:var(--muted);
    background:var(--surface2); border-radius:999px; padding:2px 8px}
  .lcard .bar{height:5px; border-radius:3px; background:var(--surface2);
    margin:7px 0 5px; overflow:hidden}
  .lcard .bar i{display:block; height:100%; border-radius:3px; background:#3ecf8e;
    transition:width .4s ease}
  .lcard.mid .bar i{background:#ffb454}
  .lcard.hot .bar i{background:#ff6b6b}
  .lcard .sub{font-size:11.5px; color:var(--muted); font-variant-numeric:tabular-nums}
  .lcard.hot{border-color:rgba(255,107,107,.38)}
  .lcard.full .sub{color:#ff9a9a}
  .limitbox .lempty{grid-column:1/-1; padding:10px 13px; border-radius:10px;
    background:var(--bg); border:1px solid var(--border); font-size:12.5px}
  .warnnote{display:flex; align-items:flex-start; gap:7px; margin-top:6px;
    color:#ffc98a; font-size:12.5px; line-height:1.45}
  .warnnote .ci{flex:none; margin-top:1px; color:#ffb454}
  .warnnote .ci svg{width:14px; height:14px}
  .btn.danger{border-color:rgba(255,107,107,.42); color:#ff9a9a}
  .btn.danger:hover{background:rgba(255,107,107,.14); border-color:#ff6b6b; color:#ffbdbd}

  /* Toggle */
  .toggle{width:46px; height:26px; border-radius:20px; background:var(--surface2);
    position:relative; cursor:pointer; flex:none; transition:background .15s; border:1px solid var(--border)}
  .toggle.on{background:var(--accent); border-color:transparent}
  .toggle::after{content:""; position:absolute; top:2px; left:2px; width:20px; height:20px;
    border-radius:50%; background:#fff; transition:left .15s}
  .toggle.on::after{left:22px}

  /* Statuspunkt vor einer Zustandszeile. Groesse und Abstand sind auf die
     .desc-Zeile abgestimmt, damit er auf der Textmitte sitzt. */
  .dot{display:inline-block; width:8px; height:8px; border-radius:50%;
    margin-right:8px; vertical-align:middle; position:relative; top:-1px;
    background:var(--muted); flex:none}
  .dot.ok{background:#3ecf8e; box-shadow:0 0 0 3px rgba(62,207,142,.18)}
  .dot.err{background:#ff6b6b; box-shadow:0 0 0 3px rgba(255,107,107,.18)}
  .dot.wait{background:#ffb454; box-shadow:0 0 0 3px rgba(255,180,84,.18);
    animation:dotpulse 1.4s ease-in-out infinite}
  .dot.off{background:#5c6068}

  /* Ladestand als kleine Batterie statt angehaengtem Text. Die Farbe traegt
     die Aussage, die Zahl bestaetigt sie nur. */
  .batt{display:inline-flex; align-items:center; gap:6px; margin-left:9px;
    padding:2px 8px 2px 6px; border-radius:999px; background:var(--bg);
    border:1px solid var(--border); font-size:11px; line-height:1;
    font-variant-numeric:tabular-nums; vertical-align:middle}
  .batt .cell{position:relative; width:20px; height:10px; border:1.5px solid currentColor;
    border-radius:3px; flex:none}
  .batt .cell::after{content:""; position:absolute; right:-4px; top:2px;
    width:2.5px; height:4px; background:currentColor; border-radius:0 2px 2px 0}
  .batt .fill{position:absolute; left:1px; top:1px; bottom:1px; min-width:1px;
    border-radius:1px; background:currentColor}
  .batt .pct{color:var(--fg)}
  .batt.ok{color:#3ecf8e}
  .batt.mid{color:#ffb454}
  .batt.low{color:#ff6b6b}
  .batt.low .cell{animation:battpulse 1.6s ease-in-out infinite}
  @keyframes battpulse{0%,100%{opacity:1} 50%{opacity:.45}}
  @keyframes dotpulse{0%,100%{opacity:1} 50%{opacity:.35}}

  .swatches{display:flex; gap:9px; flex-wrap:wrap}
  /* Die Kacheln sahen wie Dekoration aus. Reine Skalierung um 10% faellt bei
     30 px nicht auf - erst Ring + Schatten machen sichtbar, dass man klicken
     kann. Der Ring liegt aussen (box-shadow), damit die Farbflaeche selbst
     unveraendert bleibt und man sie weiter beurteilen kann. */
  .sw{width:30px; height:30px; border-radius:9px; cursor:pointer; border:2px solid transparent;
    transition:transform .13s ease, box-shadow .13s ease}
  .sw:hover{transform:scale(1.18);
    box-shadow:0 0 0 2px rgba(255,255,255,.45), 0 5px 16px rgba(0,0,0,.5)}
  .sw:active{transform:scale(1.04); transition-duration:.05s}
  .sw.active{border-color:#fff}
  .sw.active:hover{box-shadow:0 0 0 3px rgba(255,255,255,.3), 0 5px 16px rgba(0,0,0,.5)}

  select.sel-input{
    background:var(--bg); border:1px solid var(--border); color:var(--fg);
    border-radius:10px; padding:10px 12px; font-family:inherit; font-size:13.5px; outline:none; cursor:pointer;
  }
  .hiddenlist{list-style:none; margin-top:8px}
  .hiddenlist li{display:flex; align-items:center; gap:10px; padding:9px 12px;
    background:var(--bg); border:1px solid var(--border); border-radius:10px; margin-bottom:7px;
    font-family:Consolas,monospace; font-size:12.5px}
  .hiddenlist li .x{margin-left:auto; cursor:pointer; color:var(--muted); padding:2px 8px; border-radius:7px}
  .hiddenlist li .x:hover{background:#e54b58; color:#fff}
  .hiddenlist .none{color:var(--muted); font-family:inherit; justify-content:center}

  /* Scrollbar */
  ::-webkit-scrollbar{width:11px; height:11px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--surface2); border-radius:8px; border:3px solid var(--bg)}
  ::-webkit-scrollbar-thumb:hover{background:#2c3450}

  /* Onboarding */
  .onboard{position:fixed; inset:0; z-index:100; display:none; place-items:center;
    background:radial-gradient(120% 120% at 50% 0%, #1d1612, #0d0a09)}
  .onboard.show{display:grid; animation:obfade .3s ease}
  @keyframes obfade{from{opacity:0}to{opacity:1}}
  .ob-card{width:460px; max-width:88vw; background:var(--surface); border:1px solid var(--border);
    border-radius:20px; padding:30px 32px 24px; text-align:center;
    box-shadow:0 30px 80px rgba(0,0,0,.6)}
  .ob-logo{width:72px; height:72px; margin-bottom:14px}
  .ob-step h2{font-size:23px; font-weight:700; margin-bottom:10px}
  .ob-step p{color:var(--muted); font-size:14px; line-height:1.6; margin-bottom:6px}
  .ob-swatches{display:flex; flex-wrap:wrap; gap:12px; justify-content:center; margin-top:20px}
  .ob-sw{width:42px; height:42px; border-radius:12px; cursor:pointer; border:3px solid transparent;
    transition:transform .1s}
  .ob-sw:hover{transform:scale(1.12)}
  .ob-sw.active{border-color:#fff; transform:scale(1.12)}
  .ob-line{display:flex; align-items:center; justify-content:space-between; gap:14px;
    text-align:left; background:var(--bg); border:1px solid var(--border);
    border-radius:12px; padding:14px 16px; margin-top:18px}
  .ob-lbl{font-weight:600}
  .ob-desc{color:var(--muted); font-size:12.5px; margin-top:2px}
  .ob-folder{margin-top:14px; color:var(--muted); font-size:12.5px; text-align:left;
    background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:12px 16px;
    word-break:break-all}
  .ob-list{text-align:left; background:var(--bg); border:1px solid var(--border);
    border-radius:12px; padding:12px 14px; margin-top:14px; font-size:13px; max-height:260px;
    overflow-y:auto}
  .ob-list .row{display:grid; grid-template-columns:110px 1fr; gap:10px; padding:7px 4px;
    border-bottom:1px dashed var(--border)}
  .ob-list .row:last-child{border-bottom:none}
  .ob-list .k{color:var(--fg); font-weight:600}
  .ob-list .v{color:var(--muted); line-height:1.45}
  .ob-list kbd{display:inline-block; background:var(--surface2); border:1px solid var(--border);
    border-radius:5px; padding:1px 6px; font-size:11px; font-family:inherit; color:var(--fg)}
  .ob-dots{display:flex; gap:8px; justify-content:center; margin:24px 0 18px}
  .ob-dots i{width:8px; height:8px; border-radius:50%; background:var(--surface2); transition:all .2s}
  .ob-dots i.on{background:var(--accent); width:22px; border-radius:5px}
  .ob-nav{display:flex; gap:10px; justify-content:space-between}
  .ob-nav .btn{flex:1; justify-content:center}

  /* Toast (statt nativer alert-Box) */
  .toast{position:fixed; left:50%; bottom:26px; transform:translate(-50%,20px);
    background:var(--surface2); color:var(--fg); border:1px solid var(--border);
    border-radius:11px; padding:11px 18px; font-size:13.5px; font-weight:600;
    box-shadow:0 8px 30px rgba(0,0,0,.5); opacity:0; pointer-events:none;
    transition:opacity .2s, transform .2s; z-index:80; max-width:80%}
  .toast.show{opacity:1; transform:translate(-50%,0)}

  /* Popover (Farbe) */
  .overlay{position:fixed; inset:0; background:rgba(0,0,0,.5); display:none;
    align-items:center; justify-content:center; z-index:50}
  .overlay.show{display:flex}
  .pop{background:var(--surface); border:1px solid var(--border); border-radius:16px;
    padding:20px 22px; width:340px; box-shadow:0 20px 60px rgba(0,0,0,.5)}
  .pop h3{font-size:15px; margin-bottom:14px}
  .pop .swatches{margin-bottom:16px}
  .pop .actions2{display:flex; gap:9px; justify-content:flex-end}
  .modal-input{width:100%; background:var(--bg); border:1px solid var(--border); color:var(--fg);
    border-radius:10px; padding:11px 13px; font-family:inherit; font-size:14px; outline:none; margin-bottom:16px}
  .modal-input:focus{border-color:var(--accent)}

  /* eigener Farbwaehler */
  .cpick{margin:14px 0 16px}
  .cp-sv{position:relative; width:100%; height:132px; border-radius:11px; overflow:hidden;
    cursor:crosshair; touch-action:none}
  .cp-sv-white,.cp-sv-black{position:absolute; inset:0; pointer-events:none}
  .cp-sv-white{background:linear-gradient(90deg,#fff,rgba(255,255,255,0))}
  .cp-sv-black{background:linear-gradient(0deg,#000,rgba(0,0,0,0))}
  .cp-sv-dot{position:absolute; width:15px; height:15px; border-radius:50%; border:2px solid #fff;
    box-shadow:0 0 0 1.5px rgba(0,0,0,.45); transform:translate(-50%,-50%); pointer-events:none}
  .cp-hue{-webkit-appearance:none; appearance:none; width:100%; height:14px; border-radius:8px;
    margin:14px 0 0; outline:none; cursor:pointer;
    background:linear-gradient(90deg,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)}
  .cp-hue::-webkit-slider-thumb{-webkit-appearance:none; width:18px; height:18px; border-radius:50%;
    background:#fff; border:2px solid rgba(0,0,0,.35); cursor:pointer; box-shadow:0 1px 4px rgba(0,0,0,.5)}
  .cp-foot{display:flex; align-items:center; gap:10px; margin-top:13px}
  .cp-prev{width:36px; height:36px; border-radius:9px; border:1px solid var(--border); flex:none}
  .cp-hex{flex:1; background:var(--bg); border:1px solid var(--border); color:var(--fg);
    border-radius:9px; padding:9px 12px; font-family:Consolas,monospace; font-size:14px; outline:none}
  .cp-hex:focus{border-color:var(--accent)}

  /* ---- Buddy-Tab ---- */
  /* Freigestellt und randlos: das Bild ist zugeschnitten und durchsichtig,
     ein Kaestchen drumherum wuerde die Figur wieder klein wirken lassen.
     object-fit:contain haelt das Seitenverhaeltnis, falls das PNG doch mal
     nicht quadratisch ankommt. */
  /* Genau 40 px - so gross wird das PNG auch geliefert. Jede andere Groesse
     hier wuerde die Pixel wieder krumm rechnen. */
  .buddy-hp{width:40px; height:40px; image-rendering:pixelated}
  .ba-headline{display:flex; align-items:flex-start; gap:22px; justify-content:space-between}
  .ba-headline > div:first-child{flex:1}
  /* Alles unterhalb des Haupt-Schalters. Ist der Buddy aus, bleibt es
     sichtbar (man soll ja sehen was einen erwartet), aber deutlich
     zurueckgenommen und nicht bedienbar - sonst dreht man an Reglern
     ohne Wirkung. */
  .ba-sub{transition:opacity .18s ease}
  .ba-sub.off{opacity:.38; filter:grayscale(.75); pointer-events:none}
  .ba-off-hint{display:none; align-items:center; gap:10px; margin:0 0 14px;
    padding:11px 14px; border-radius:10px;
    background:color-mix(in srgb, var(--accent) 10%, transparent);
    border:1px solid color-mix(in srgb, var(--accent) 28%, transparent);
    color:var(--text); font-size:13px}
  .ba-off-hint.show{display:flex}
  .ba-toggle{display:flex; flex-direction:column; align-items:center; gap:6px}
  .ba-toggle-lbl{font-size:12px; color:var(--muted); letter-spacing:.02em}
  .ba-vis{display:flex; flex-direction:column; gap:10px; margin-bottom:12px}
  .ba-radio{display:flex; align-items:center; gap:10px; cursor:pointer; user-select:none; font-size:14px}
  .ba-radio input{accent-color:var(--accent); width:16px; height:16px}
  .ba-radio .ba-dim{color:var(--muted); font-style:normal; font-size:12px}
  .ba-radio .ba-dim code{background:var(--bg); padding:1px 6px; border-radius:4px; border:1px solid var(--border); font-family:Consolas,monospace}
  .ba-window{display:flex; gap:9px; margin-top:6px; transition:opacity .15s}
  .ba-window.disabled{opacity:.4; pointer-events:none}
  .ba-window input{flex:1; background:var(--bg); border:1px solid var(--border); color:var(--fg);
    border-radius:9px; padding:9px 12px; font-family:inherit; font-size:13.5px; outline:none}
  .ba-window input:focus{border-color:var(--accent)}
  .ba-hint{color:var(--muted); font-size:12px; margin-top:8px}
  .ba-slider{display:flex; flex-direction:column; gap:6px; margin:12px 0}
  .ba-slider label{font-size:13px; color:var(--muted); display:flex; justify-content:space-between}
  .ba-slider .ba-val{color:var(--fg); font-family:Consolas,monospace}
  .ba-slider input[type=range]{width:100%; accent-color:var(--accent)}
  .ba-grid{display:grid; grid-template-columns:repeat(auto-fill, minmax(94px, 1fr)); gap:10px;
    margin-top:8px}
  .ba-cell{background:var(--bg); border:1px solid var(--border); border-radius:10px;
    padding:8px; text-align:center; cursor:pointer; transition:transform .08s, border-color .12s}
  .ba-cell:hover{transform:translateY(-1px); border-color:var(--accent)}
  .ba-cell.active{border-color:var(--accent); box-shadow:0 0 0 2px color-mix(in srgb, var(--accent) 32%, transparent)}
  .ba-cell img{width:80px; height:80px; image-rendering:pixelated; image-rendering:crisp-edges;
    display:block; margin:0 auto; border-radius:6px; background:#14100e}
  .ba-name{font-size:11px; color:var(--muted); margin-top:6px; text-transform:lowercase; letter-spacing:.02em}
  .ba-actions{display:flex; justify-content:space-between; align-items:center; gap:16px; margin-top:14px}
  .ba-party{display:flex; align-items:center; gap:10px; font-size:13.5px; color:var(--muted)}
  .ba-wlist{overflow:auto; max-height:50vh; border:1px solid var(--border); border-radius:10px;
    background:var(--bg); margin-bottom:12px}
  .ba-wlist-row{padding:9px 12px; cursor:pointer; border-bottom:1px solid var(--border);
    font-size:13.5px; color:var(--fg); white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .ba-wlist-row:last-child{border-bottom:none}
  .ba-wlist-row:hover{background:var(--surface2)}
  .ba-anchor-row{margin:14px 0 8px; display:flex; align-items:center; gap:16px; flex-wrap:wrap}
  .ba-anchor-lbl{font-size:13px; color:var(--muted); min-width:100px}
  .ba-monitor-tabs{display:flex; gap:6px; flex-wrap:wrap}
  .ba-monitor-tab{background:var(--bg); border:1px solid var(--border); border-radius:7px;
    padding:6px 10px; font-size:12px; color:var(--muted); cursor:pointer; font-family:inherit;
    transition:all .1s}
  .ba-monitor-tab:hover{border-color:var(--accent); color:var(--fg)}
  .ba-monitor-tab.active{background:var(--accent); color:#fff; border-color:transparent}
  .ba-anchor-grid{display:grid; grid-template-columns:repeat(3, 26px);
    grid-template-rows:repeat(3, 22px); gap:3px}
  .ba-anchor{background:var(--bg); border:1px solid var(--border); border-radius:6px;
    cursor:pointer; padding:0; position:relative; transition:all .1s}
  .ba-anchor::after{content:""; position:absolute; width:8px; height:8px; border-radius:2px;
    background:var(--muted); top:50%; left:50%; transform:translate(-50%,-50%); transition:background .1s}
  .ba-anchor:hover{border-color:var(--accent)}
  .ba-anchor:hover::after{background:var(--accent)}
  .ba-anchor:nth-child(1)::after{left:16%; top:16%; transform:none}
  .ba-anchor:nth-child(2)::after{left:50%; top:16%; transform:translate(-50%,0)}
  .ba-anchor:nth-child(3)::after{left:auto; right:16%; top:16%; transform:none}
  .ba-anchor:nth-child(4)::after{left:16%; top:50%; transform:translate(0,-50%)}
  .ba-anchor:nth-child(5)::after{left:50%; top:50%; transform:translate(-50%,-50%)}
  .ba-anchor:nth-child(6)::after{left:auto; right:16%; top:50%; transform:translate(0,-50%)}
  .ba-anchor:nth-child(7)::after{left:16%; top:auto; bottom:16%; transform:none}
  .ba-anchor:nth-child(8)::after{left:50%; top:auto; bottom:16%; transform:translate(-50%,0)}
  .ba-anchor:nth-child(9)::after{left:auto; right:16%; top:auto; bottom:16%; transform:none}
  .ba-pos-hint{color:var(--muted); font-size:12.5px}
  .ba-pos-hint code{background:var(--bg); padding:2px 8px; border-radius:5px; border:1px solid var(--border); color:var(--fg)}
  .ba-frame-row{display:flex; align-items:center; gap:20px; margin:14px 0 6px; flex-wrap:wrap}
  .ba-frame-styles{display:flex; align-items:center; gap:8px}
  .ba-frame-lbl{font-size:13.5px; color:var(--fg); margin-right:4px}
  .ba-style{background:var(--bg); border:1px solid var(--border); color:var(--fg); border-radius:7px;
    padding:6px 12px; font-family:inherit; font-size:13px; cursor:pointer; transition:all .1s}
  .ba-style:hover{border-color:var(--accent)}
  .ba-style.active{background:var(--accent); color:#fff; border-color:transparent}
  .ba-frame-colors{display:flex; gap:6px; transition:opacity .15s}
  .ba-frame-colors.dim{opacity:.35; pointer-events:none}
  .ba-fc{width:22px; height:22px; border-radius:6px; cursor:pointer; border:2px solid transparent;
    transition:transform .13s ease, box-shadow .13s ease}
  .ba-fc:hover{transform:scale(1.2);
    box-shadow:0 0 0 2px rgba(255,255,255,.45), 0 4px 12px rgba(0,0,0,.5)}
  .ba-fc:active{transform:scale(1.05); transition-duration:.05s}
  .ba-fc:hover{transform:scale(1.15)}
  .ba-fc.active{border-color:#fff; box-shadow:0 0 0 1px rgba(0,0,0,.4)}
  .ba-frame-label{margin:6px 0 6px; transition:opacity .15s}
  .ba-frame-label.hidden{display:none}
  .ba-frame-label label{font-size:13px; color:var(--muted); display:flex; align-items:center; gap:10px}
  .ba-frame-label input{background:var(--bg); border:1px solid var(--border); color:var(--fg);
    border-radius:8px; padding:6px 10px; font-family:Consolas,monospace; font-size:13px;
    letter-spacing:.05em; outline:none; width:160px; text-transform:uppercase}
  .ba-frame-label input:focus{border-color:var(--accent)}
</style>
</head>
<body>
<div class="app">
  <!-- Titelleiste -->
  <!-- SVG-Sprite (Logo-Definition, einmal) -->
  <svg width="0" height="0" style="position:absolute" aria-hidden="true">
    <defs>
      <radialGradient id="coral" cx="50%" cy="46%" r="65%">
        <stop offset="0%" stop-color="#F08660"/><stop offset="60%" stop-color="#EC7456"/><stop offset="100%" stop-color="#E2654A"/>
      </radialGradient>
      <polygon id="rayLong" points="0,-470 17,-426 23,-180 9,-50 -9,-50 -23,-180 -17,-426" fill="url(#coral)"/>
      <polygon id="rayShort" points="0,-432 16,-392 22,-170 9,-50 -9,-50 -22,-170 -16,-392" fill="url(#coral)"/>
      <g id="rays" transform="translate(512 512)">
        <use href="#rayLong" transform="rotate(0)"/><use href="#rayShort" transform="rotate(18)"/>
        <use href="#rayLong" transform="rotate(36)"/><use href="#rayShort" transform="rotate(54)"/>
        <use href="#rayLong" transform="rotate(72)"/><use href="#rayShort" transform="rotate(90)"/>
        <use href="#rayLong" transform="rotate(108)"/><use href="#rayShort" transform="rotate(126)"/>
        <use href="#rayLong" transform="rotate(144)"/><use href="#rayShort" transform="rotate(162)"/>
        <use href="#rayLong" transform="rotate(180)"/><use href="#rayShort" transform="rotate(198)"/>
        <use href="#rayLong" transform="rotate(216)"/><use href="#rayShort" transform="rotate(234)"/>
        <use href="#rayLong" transform="rotate(252)"/><use href="#rayShort" transform="rotate(270)"/>
        <use href="#rayLong" transform="rotate(288)"/><use href="#rayShort" transform="rotate(306)"/>
        <use href="#rayLong" transform="rotate(324)"/><use href="#rayShort" transform="rotate(342)"/>
        <circle r="170" fill="url(#coral)"/>
      </g>
    </defs>
  </svg>

  <!-- Tabs -->
  <div class="tabs">
    <div class="tab active" data-view="sessions" onclick="switchView('sessions')">Sessions</div>
    <div class="tab" data-view="buddy" onclick="switchView('buddy')">Buddy</div>
    <div class="tab" data-view="settings" onclick="switchView('settings')">Einstellungen</div>
  </div>

  <!-- Update-Hinweis (nur sichtbar wenn Update verfuegbar) -->
  <div class="updatebar" id="updatebar">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg>
    <span class="utext"></span>
    <span class="unotes"></span>
    <button class="btn accent mini" onclick="openUpdateDialog()">Details ansehen</button>
    <button class="btn mini" onclick="dismissUpdate()">Später</button>
  </div>

  <!-- Sessions -->
  <div class="view active" id="view-sessions">
    <div class="head">
      <h1 class="titlewrap"><svg class="hlogo" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
        <g class="l-spin"><use href="#rays"/></g></svg><span><span class="g">Claude</span> Sessions</span></h1>
      <div class="count" id="count"></div>
    </div>
    <div class="searchbar">
      <label class="search">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
        <input id="search" placeholder="Suche nach Titel, Ordner, Inhalt …" autocomplete="off">
      </label>
      <button class="btn" onclick="doRefresh(this)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>
        Aktualisieren
      </button>
    </div>

    <div class="main">
      <div class="table">
        <div class="thead" id="thead"></div>
        <div class="tbody" id="tbody"></div>
      </div>

      <aside class="side">
        <div class="detail" id="detail">Wähle eine Session aus, um Details zu sehen.</div>
        <div class="actions">
          <button class="btn accent" id="btn-resume" disabled onclick="doResume()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5v14l12-7z"/></svg>
            In Session einsteigen
          </button>
          <!-- Zweitrangige Aktionen als Reihe statt drei volle Knoepfe
               untereinander. Beschriftung bleibt drunter stehen - ein reines
               Symbolfeld waere zwar kleiner, aber "Farbe" und "ID kopieren"
               errraet man an einem Symbol nicht zuverlaessig. -->
          <div class="iconrow">
            <button class="iconbtn" id="btn-rename" disabled onclick="openRename()"
                    title="Titel ändern (F2)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
              <span>Titel</span>
            </button>
            <button class="iconbtn" id="btn-color" disabled onclick="openColor()"
                    title="Farbe der Session festlegen">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="8.5" cy="10.5" r="1.2" fill="currentColor"/><circle cx="12" cy="8" r="1.2" fill="currentColor"/><circle cx="15.5" cy="10.5" r="1.2" fill="currentColor"/></svg>
              <span>Farbe</span>
            </button>
            <button class="iconbtn" id="btn-copy" disabled onclick="doCopy()"
                    title="Session-ID in die Zwischenablage kopieren">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
              <span>ID</span>
            </button>
          </div>
        </div>
      </aside>
    </div>
  </div>

  <!-- Buddy -->
  <div class="view" id="view-buddy">
    <div class="head">
      <h1 class="titlewrap">
        <span class="hlogo" style="width:40px;height:40px;display:inline-flex;align-items:center;justify-content:center">
          <img id="buddy-heading-preview" class="buddy-hp" alt="">
        </span>
        <span><span class="g">Dein</span> Claude-Buddy</span>
      </h1>
      <div class="count" id="buddy-status"></div>
    </div>
    <div class="settings" id="buddy-panel"></div>
  </div>

  <!-- Einstellungen -->
  <div class="view" id="view-settings">
    <div class="head"><h1>Einstellungen</h1></div>
    <div class="jumpbar" id="settings-jump"></div>
    <div class="settings" id="settings"></div>
  </div>

  <div class="shortcutbar" id="shortcutbar"></div>
</div>

<div class="toast" id="toast"></div>

<!-- Onboarding (nur beim ersten Start) -->
<div class="onboard" id="onboard">
  <div class="ob-card">
    <svg class="ob-logo" viewBox="0 0 1024 1024"><g class="l-spin"><use href="#rays"/></g></svg>

    <div class="ob-step" data-step="0">
      <h2 id="ob-title">Willkommen 👋</h2>
      <p id="ob-intro">Dein Browser für alle lokalen Claude-Code-Sessions – durchsuchen, einfärben und per Klick wieder einsteigen. Lass uns kurz einrichten – dauert nur eine Minute.</p>
    </div>

    <div class="ob-step" data-step="1" hidden>
      <h2>Wähle deine Farbe</h2>
      <p>Die Akzentfarbe der Oberfläche. Du kannst sie später jederzeit in den Einstellungen ändern.</p>
      <div class="ob-swatches" id="ob-swatches"></div>
    </div>

    <div class="ob-step" data-step="2" hidden>
      <h2>Die Spalten</h2>
      <p>Das siehst du für jede Session. Alle Spalten kannst du in den Einstellungen ein-/ausblenden und die Reihenfolge ändern.</p>
      <div class="ob-list">
        <div class="row"><div class="k">Titel</div><div class="v">Automatisch erzeugte Kurzbeschreibung der Session – oder dein selbst vergebener Name.</div></div>
        <div class="row"><div class="k">Ordner</div><div class="v">Das Arbeitsverzeichnis, in dem die Session gestartet wurde.</div></div>
        <div class="row"><div class="k">Nachrichten</div><div class="v">Anzahl ausgetauschter Nachrichten – gute Anhaltszahl für den Umfang.</div></div>
        <div class="row"><div class="k">Zuletzt aktiv</div><div class="v">Wann du zuletzt mit der Session gearbeitet hast (heute / gestern / Datum).</div></div>
        <div class="row"><div class="k">Session-ID</div><div class="v">Interne ID (standardmäßig ausgeblendet). Praktisch zum Suchen.</div></div>
        <div class="row"><div class="k">Erste Frage</div><div class="v">Deine allererste Nachricht der Session (standardmäßig ausgeblendet).</div></div>
      </div>
    </div>

    <div class="ob-step" data-step="3" hidden>
      <h2>So geht's schnell</h2>
      <p>Die wichtigsten Handgriffe – der Rest ergibt sich beim Ausprobieren.</p>
      <div class="ob-list">
        <div class="row"><div class="k">Doppelklick</div><div class="v">Öffnet die Session direkt in Claude Code – der schnellste Weg zurück in ein Gespräch.</div></div>
        <div class="row"><div class="k"><kbd>Enter</kbd></div><div class="v">Öffnet die aktuell markierte Session (wenn das Suchfeld nicht aktiv ist).</div></div>
        <div class="row"><div class="k"><kbd>F2</kbd></div><div class="v">Session umbenennen – der Titel bleibt dauerhaft dein eigener.</div></div>
        <div class="row"><div class="k">Rechtsklick</div><div class="v">Öffnet das Menü mit Farbe, Umbenennen und Ordner ausblenden.</div></div>
        <div class="row"><div class="k">Suche</div><div class="v">Filtert live nach Titel, Ordner, ID oder erster Frage – auch mit mehreren Wörtern.</div></div>
        <div class="row"><div class="k"><kbd>F11</kbd></div><div class="v">Vollbild an/aus.</div></div>
      </div>
    </div>

    <div class="ob-step" data-step="4" hidden>
      <h2>Neu: Dein Clawd-Buddy ✨</h2>
      <p>Ein winziger animierter Clawd (20×20 Pixel) schwebt auf dem Desktop und zeigt, was gerade passiert – schreibt Claude gerade Code, denkt er nach, wurde ein Limit erreicht? Standardmäßig taucht er nur auf wenn Claude Code läuft, blendet sich weich rein und wieder aus.</p>
      <div class="ob-list">
        <div class="row"><div class="k">Aktivieren</div><div class="v">Tab „Buddy" → Toggle „An". Beim ersten Mal steht er in der Bildschirmmitte.</div></div>
        <div class="row"><div class="k">Platzieren</div><div class="v">Ecken/Kanten per Schnellwahl (auf jedem Monitor) oder „Buddy platzieren…" für freies Ziehen mit Raster.</div></div>
        <div class="row"><div class="k">Aussehen</div><div class="v">Größe 40–200 px, Deckkraft, optionaler Rahmen in deiner Wunschfarbe.</div></div>
        <div class="row"><div class="k">Rechtsklick</div><div class="v">Rechtsklick oder Doppelklick auf den Buddy schickt ihn kurz weg – ausgeschaltet wird er dadurch nicht. Beim nächsten neuen Claude-Terminal ist er wieder da.</div></div>
      </div>
    </div>

    <div class="ob-step" data-step="5" hidden>
      <h2>Fast geschafft</h2>
      <div class="ob-line">
        <div><div class="ob-lbl">Heimatordner ausblenden</div>
          <div class="ob-desc">Sessions, die direkt in deinem Benutzerordner (<code>C:\Users\...</code>) laufen, verstecken. Standardmäßig aus – aktiviere es nur, wenn dich diese Sessions stören.</div></div>
        <div class="toggle" id="ob-home" onclick="obToggleHome(this)"></div>
      </div>
      <div class="ob-folder" id="ob-folder"></div>
    </div>

    <div class="ob-dots" id="ob-dots"></div>
    <div class="ob-nav">
      <button class="btn" id="ob-back" onclick="obPrev()" style="visibility:hidden">Zurück</button>
      <button class="btn accent" id="ob-next" onclick="obNext()">Weiter</button>
    </div>
  </div>
</div>

<!-- Overlays -->
<div class="overlay" id="overlay-color">
  <div class="pop">
    <h3>Farbe für diese Session</h3>
    <div class="swatches" id="color-swatches"></div>
    <div class="cpick">
      <div class="cp-sv" id="cp-sv" onpointerdown="cpDown(event)">
        <div class="cp-sv-white"></div><div class="cp-sv-black"></div>
        <div class="cp-sv-dot" id="cp-sv-dot"></div>
      </div>
      <input type="range" min="0" max="360" value="250" class="cp-hue" id="cp-hue"
             oninput="CP.h=+this.value; cpRender()">
      <div class="cp-foot">
        <span class="cp-prev" id="cp-prev"></span>
        <input class="cp-hex" id="cp-hex" maxlength="7" onchange="cpHexIn(this.value)">
      </div>
    </div>
    <div class="actions2">
      <button class="btn" onclick="setColor('')">Keine</button>
      <button class="btn" onclick="closeOverlay('overlay-color')">Abbrechen</button>
      <button class="btn accent" onclick="setColor(cpHex())">Übernehmen</button>
    </div>
  </div>
</div>

<div class="overlay" id="overlay-rename">
  <div class="pop">
    <h3>Titel ändern</h3>
    <input class="modal-input" id="rename-input" placeholder="Neuer Titel">
    <div class="actions2">
      <button class="btn" onclick="resetTitle()" title="Umbenennung rückgängig – zeigt wieder den automatisch erzeugten Titel">Standard-Titel</button>
      <button class="btn" onclick="closeOverlay('overlay-rename')">Abbrechen</button>
      <button class="btn accent" onclick="saveRename()">Speichern</button>
    </div>
  </div>
</div>

<div class="overlay" id="overlay-buddy-win">
  <div class="pop" style="width:520px; max-height:70vh; display:flex; flex-direction:column">
    <h3>Fenster auswählen</h3>
    <div class="sub" style="margin-bottom:10px">Der Buddy erscheint nur, wenn das gewählte Fenster gerade im Vordergrund ist.</div>
    <div class="ba-wlist"></div>
    <div class="actions2">
      <button class="btn" onclick="closeOverlay('overlay-buddy-win')">Abbrechen</button>
    </div>
  </div>
</div>

<div class="overlay" id="overlay-update">
  <div class="pop" id="upd-pop" style="width:460px">
    <div id="upd-info">
      <h3 id="upd-title">Update verfügbar</h3>
      <div id="upd-notes"></div>
      <div class="upd-keep">Deine Einstellungen, Farben und Titel bleiben dabei vollständig erhalten.</div>
      <div class="actions2">
        <button class="btn" onclick="closeOverlay('overlay-update')">Später</button>
        <button class="btn accent" id="upd-install" onclick="doInstall()">Jetzt installieren</button>
      </div>
    </div>
    <div id="upd-progress">
      <div class="inst-stage">
        <div class="confetti" id="confetti"></div>
        <svg class="inst-logo" viewBox="0 0 1024 1024"><use href="#rays"/></svg>
        <svg class="inst-check" viewBox="0 0 52 52"><circle cx="26" cy="26" r="23"/><path d="M15 27l7 7 15-16"/></svg>
      </div>
      <div class="inst-state" id="inst-state">Lädt herunter…</div>
      <div class="bar"><div class="bar-fill" id="bar-fill"></div><div class="bar-shine"></div></div>
      <div class="inst-pct" id="inst-pct">0%</div>
    </div>
  </div>
</div>

<script>
/* ---- Sprache -------------------------------------------------------------
   Der deutsche Satz ist der Schluessel. Fehlt eine Uebersetzung, steht dort
   Deutsch - die Oberflaeche bleibt bedienbar, auch waehrend die Tabelle noch
   waechst. Dieselbe Tabelle benutzt der Python-Teil.                        */
let I18N = __I18N__;
function t(text, vars){
  let out = (I18N.table && I18N.table[text]) || text;
  if(vars) out = out.replace(/\{(\w+)\}/g, (m,k)=> (k in vars ? vars[k] : m));
  return out;
}
/* Festes Markup

   Statt jede der rund sechzig Stellen von Hand zu markieren, wird das
   Grundgeruest einmal beim Start durchlaufen und der deutsche Wortlaut
   gemerkt. Das geht nur, solange die Tabellen und Panels noch leer sind -
   deshalb laeuft collectStaticT() vor dem ersten Zeichnen. Alles, was
   danach entsteht, uebersetzt sich ueber t() beim Aufbauen selbst.

   Gemerkt wird der deutsche Ausgangstext, nicht der angezeigte: sonst
   liesse sich nach dem Umschalten auf Englisch nichts mehr nachschlagen. */
/* Frisch aufgebaute Bereiche uebersetzen.

   Einstellungen und Buddy-Seite entstehen als grosse Vorlagen. Statt dort
   jede Ueberschrift und jeden Beschreibungssatz einzeln zu umklammern, laeuft
   nach dem Aufbauen ein Durchgang darueber: was genau einem Eintrag der
   Tabelle entspricht, wird ersetzt. Der deutsche Wortlaut bleibt dadurch im
   Code stehen und ist beim Lesen sofort sichtbar.

   Uebersprungen wird alles unter data-raw - dort stehen Sessiontitel, Pfade
   und Fenstertitel. Hiesse eine Session zufaellig „Suche", wuerde sie sonst
   im Englischen als „Search" auftauchen. */
function translateDom(root){
  if(!root || !I18N.table) return;
  const lauf = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n){
      for(let p = n.parentElement; p && p !== root; p = p.parentElement){
        if(p.hasAttribute('data-raw')) return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const treffer = [];
  for(let n = lauf.nextNode(); n; n = lauf.nextNode()){
    const kern = n.nodeValue && n.nodeValue.trim();
    if(kern && I18N.table[kern]) treffer.push([n, kern]);
  }
  treffer.forEach(([n, kern])=>{
    n.nodeValue = n.nodeValue.replace(kern, I18N.table[kern]);
  });
  root.querySelectorAll('[title],[placeholder]').forEach(el=>{
    ['title','placeholder'].forEach(a=>{
      const v = el.getAttribute(a);
      if(v && I18N.table[v]) el.setAttribute(a, I18N.table[v]);
    });
  });
}

let STATIC_T = [];
function collectStaticT(){
  STATIC_T = [];
  const lauf = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for(let n = lauf.nextNode(); n; n = lauf.nextNode()){
    const roh = n.nodeValue;
    if(!roh || !roh.trim()) continue;
    const eltern = n.parentNode;
    if(eltern && (eltern.tagName === 'SCRIPT' || eltern.tagName === 'STYLE')) continue;
    STATIC_T.push({node:n, de:roh});
  }
  document.querySelectorAll('[placeholder]').forEach(el=>{
    STATIC_T.push({el:el, attr:'placeholder', de:el.getAttribute('placeholder')});
  });
  document.querySelectorAll('[title]').forEach(el=>{
    STATIC_T.push({el:el, attr:'title', de:el.getAttribute('title')});
  });
}
function applyStaticT(){
  STATIC_T.forEach(e=>{
    // Fuehrende und folgende Leerzeichen erhalten - sonst kleben Texte, die
    // im Markup neben einem Symbol stehen, ploetzlich daran fest.
    if(e.node){
      const kern = e.de.trim();
      const uebersetzt = t(kern);
      if(uebersetzt !== kern) e.node.nodeValue = e.de.replace(kern, uebersetzt);
    }else if(e.el && e.de){
      e.el.setAttribute(e.attr, t(e.de));
    }
  });
  document.documentElement.setAttribute('lang', I18N.lang || 'de');
}
async function setLanguage(code){
  try{ I18N = await api.set_language(code); }catch(e){ return; }
  // Auch im hiesigen Abbild nachziehen: die Einstellungsseite baut die
  // Auswahlbox daraus auf. Ohne das sprang sie nach jedem Wechsel zurueck
  // auf „Automatisch", obwohl gespeichert war, was du gewaehlt hast.
  if(STATE && STATE.settings) STATE.settings.language = code;
  // Die Spalte „Zuletzt aktiv" entsteht im Python-Teil und liegt schon
  // fertig im Abbild - „gestern 13:14" bliebe sonst stehen, waehrend
  // ringsum alles englisch ist. Einmal neu holen rechnet sie um.
  try{ ingest(await api.get_state()); }catch(e){}
  applyStaticT();
  renderAll();
}

const COLORS = ["#4aa3ff","#3ecf8e","#ffb454","#ff6b6b","#c08cff","#ffe066","#34d6c8","#ff8fcf"];
const ALL_COLS = {
  title:   {label:"Titel",         grow:"2.6fr"},
  project: {label:"Ordner",        grow:"2fr"},
  msgs:    {label:"Nachrichten",   grow:"1.1fr", num:true},
  when:    {label:"Zuletzt aktiv", grow:"1fr"},
  id:      {label:"Session-ID",    grow:"1.7fr"},
  first:   {label:"Erste Frage",   grow:"2.4fr"},
};
const DEFAULT_ON = {title:true, project:true, msgs:true, when:true, id:false, first:false};
function normCols(){
  const saved=(STATE && STATE.settings.columns)||[];
  const order=saved.map(c=>c.key).filter(k=>ALL_COLS[k]);
  Object.keys(ALL_COLS).forEach(k=>{ if(!order.includes(k)) order.push(k); });
  return order.map(k=>{const f=saved.find(c=>c.key===k); return {key:k, on: f?!!f.on:DEFAULT_ON[k]};});
}
function visCols(){ return normCols().filter(c=>c.on).map(c=>({key:c.key, ...ALL_COLS[c.key]})); }
function applyCols(){ document.documentElement.style.setProperty('--cols', visCols().map(c=>c.grow).join(' ')); }
function cellHtml(s,key){
  switch(key){
    case 'title':   return `<div class="cell title">${esc(s.display_title)}</div>`;
    case 'project': return `<div class="cell dim">${esc(s.cwd||s.project)}</div>`;
    case 'msgs':    return `<div class="cell"><span class="ic">${SVG_MSG}${s.total_msgs} <span class="dim">(${s.user_msgs}/${s.assistant_msgs})</span></span></div>`;
    case 'when':    return `<div class="cell"><span class="ic">${SVG_CAL}${esc(s.when)}</span></div>`;
    case 'id':      return `<div class="cell dim mono">${esc(s.id)}</div>`;
    case 'first':   return `<div class="cell dim">${esc(s.first_user||'—')}</div>`;
  }
  return '<div class="cell"></div>';
}
const SVG_MSG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.4 8.4 0 0 1-12 7.6L3 21l1.9-5.6A8.4 8.4 0 1 1 21 11.5z"/></svg>';
const SVG_CAL = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4.5" width="18" height="17" rx="2.5"/><path d="M3 9h18M8 2.5v4M16 2.5v4"/></svg>';

let STATE = null, sessions = [], selected = null;
let sortCol = "when", sortRev = true;
let api = window.pywebview ? window.pywebview.api : null;

function lum(hex){const h=hex.replace('#','');const r=parseInt(h.substr(0,2),16),g=parseInt(h.substr(2,2),16),b=parseInt(h.substr(4,2),16);return .299*r+.587*g+.114*b;}
function esc(s){return (s||'').replace(/[&<>"'`]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;'}[c]));}

// Icons fuer die Kartenueberschriften. Bewusst inline und stroke-basiert:
// die App laeuft offline und darf nichts nachladen, und so faerben sich die
// Icons ueber currentColor automatisch mit der Akzentfarbe mit.
const ICONS={
  folder:'<path d="M3 6a1 1 0 0 1 1-1h4l2 2h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
  eye:'<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.5"/>',
  hidden:'<path d="M4 4l16 16"/><path d="M9.9 5.2A9.7 9.7 0 0 1 12 5c6.5 0 10 6 10 6a17 17 0 0 1-3.2 3.8"/><path d="M6.2 8.2A17 17 0 0 0 2 11s3.5 6 10 6a9.6 9.6 0 0 0 3.6-.7"/>',
  columns:'<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="16" rx="1"/><rect x="17" y="4" width="4" height="16" rx="1"/>',
  window:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>',
  power:'<path d="M12 3v9"/><path d="M6.5 6.5a8 8 0 1 0 11 0"/>',
  bell:'<path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  gauge:'<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-4.5"/>',
  globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18"/>',
  // Tropfen statt Palette, halbgefuellter Kreis statt Kontrastraster: die
  // detailreicheren Varianten waren bei 16 px nicht mehr zu erkennen.
  palette:'<path d="M12 3.5s6 6.6 6 10.1a6 6 0 0 1-12 0c0-3.5 6-10.1 6-10.1z"/>',
  contrast:'<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor" stroke="none"/>',
  terminal:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 10l2.5 2.5L7 15"/><path d="M12.5 15H17"/>',
  bluetooth:'<path d="M7 8l10 8-5 4V4l5 4-10 8"/>',
  update:'<path d="M12 4v10"/><path d="M8 11l4 4 4-4"/><path d="M4 19h16"/>',
  buddy:'<circle cx="12" cy="12" r="8"/><circle cx="9.5" cy="10.5" r="1"/><circle cx="14.5" cy="10.5" r="1"/><path d="M9.5 15h5"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  wand:'<path d="M15 4l5 5"/><path d="M4 20L16 8"/><path d="M18 3v3"/><path d="M21 6h-3"/>',
  play:'<circle cx="12" cy="12" r="9"/><path d="M10 8.5l6 3.5-6 3.5z"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.8v.4"/>',
  link:'<path d="M10 13a4 4 0 0 0 5.7.3l3-3a4 4 0 1 0-5.7-5.7l-1.7 1.7"/><path d="M14 11a4 4 0 0 0-5.7-.3l-3 3a4 4 0 1 0 5.7 5.7l1.7-1.7"/>',
  warn:'<path d="M12 4.5L21 19H3z"/><path d="M12 10v4"/><path d="M12 16.7v.3"/>',
};
function ic(k){
  const p=ICONS[k]; if(!p) return '';
  return '<span class="ci"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
       + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+p+'</svg></span>';
}

// Hintergrund-Palette aus einem Grundton ableiten (alle Dunkelstufen)
const BG_TONES=[
  {key:'warm',  name:'Warm',    base:'#4a3a30'},
  {key:'neutral',name:'Neutral', base:'#3a3a3a'},
  {key:'cool',  name:'Kühl',    base:'#333c4f'},
  {key:'ocean', name:'Ozean',   base:'#2c4452'},
  {key:'violet',name:'Violett', base:'#3f3556'},
  {key:'forest',name:'Wald',    base:'#324a3a'},
  {key:'black', name:'Schwarz', base:'#2a2a2a'},
];
function shade(base,f){const h=base.replace('#','');
  let r=parseInt(h.substr(0,2),16),g=parseInt(h.substr(2,2),16),b=parseInt(h.substr(4,2),16);
  const z=x=>('0'+Math.max(0,Math.min(255,Math.round(x*f))).toString(16)).slice(-2);
  return '#'+z(r)+z(g)+z(b);}
function applyBg(base){const S=document.documentElement.style;
  S.setProperty('--bg',      shade(base,0.30));
  S.setProperty('--row',     shade(base,0.38));
  S.setProperty('--row-alt', shade(base,0.46));
  S.setProperty('--surface', shade(base,0.50));
  S.setProperty('--surface2',shade(base,0.74));
  S.setProperty('--border',  shade(base,0.62));
  S.setProperty('--select',  shade(base,0.92));}

function applyAccent(c){document.documentElement.style.setProperty('--accent',c);
  // helle Variante
  const h=c.replace('#','');let r=parseInt(h.substr(0,2),16),g=parseInt(h.substr(2,2),16),b=parseInt(h.substr(4,2),16);
  r=Math.min(255,r+25);g=Math.min(255,g+25);b=Math.min(255,b+25);
  document.documentElement.style.setProperty('--accent2',`rgb(${r},${g},${b})`);}

let BOOTED=false;
async function boot(){
  if(BOOTED) return; BOOTED=true;
  try{
    STATE = await api.get_state();
    sortCol = STATE.settings.sort_col || "when";
    sortRev = STATE.settings.sort_rev !== false;
    applyAccent(STATE.settings.accent || "#ec7456");
    applyBg(STATE.settings.bg_base || "#4a3a30");
    ingest(STATE);
    // Erst einsammeln, dann uebersetzen - beides vor dem ersten Zeichnen,
    // solange Tabelle und Panels noch leer sind.
    collectStaticT();
    applyStaticT();
    buildSwatches();
    renderHead();
    render();
    renderSettings();
    renderShortcutBar('sessions');   // Startansicht
    // Onboarding zeigen bei Erstinstallation ODER wenn seit dem letzten Anzeigen
    // eine neue Onboarding-Version hinzugekommen ist (nach Update). Einstellungen
    // werden dabei nicht angetastet – die Schritte spiegeln nur die aktuellen Werte.
    if(!STATE.settings.onboarded ||
       (STATE.onboarding_version && STATE.settings.onboarded_version !== STATE.onboarding_version)){
      obShow();
    }
    checkUpdate();   // im Hintergrund, blockiert nichts
  }catch(e){
    BOOTED=false; bootTries=(bootTries||0)+1;
    const c=document.getElementById('count');
    if(bootTries<5){ if(c)c.textContent=t('Lädt erneut…'); setTimeout(()=>{ if(!BOOTED) boot(); }, 700); }
    else if(c){ c.textContent=t('Fehler beim Laden: {grund}', {grund: e}); }
  }
}
let bootTries=0;

function ingest(st){STATE=st; sessions=st.sessions||[];}

let _toastT=null;
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(_toastT); _toastT=setTimeout(()=>el.classList.remove('show'), 2600);
}

function visible(){
  const q=document.getElementById('search').value.toLowerCase().trim();
  const hideHome=STATE.settings.hide_home, home=(STATE.home||'').toLowerCase();
  const hidden=(STATE.settings.hidden_folders||[]).map(f=>(f||'').toLowerCase());
  let rows=sessions.filter(s=>{
    const cwd=(s.cwd||'').toLowerCase();
    if(hideHome && cwd===home) return false;
    if(hidden.some(h=>cwd===h)) return false;
    if(q){const hay=((s.display_title||'')+' '+(s.project||'')+' '+(s.cwd||'')+' '+(s.first_user||'')+' '+(s.id||'')).toLowerCase();
      if(!hay.includes(q)) return false;}
    return true;
  });
  const key={title:s=>s.display_title.toLowerCase(),project:s=>(s.cwd||s.project).toLowerCase(),
    msgs:s=>s.total_msgs,when:s=>s.mtime,id:s=>s.id.toLowerCase(),
    first:s=>(s.first_user||'').toLowerCase()}[sortCol] || (s=>s.mtime);
  rows.sort((a,b)=>{const x=key(a),y=key(b);return (x<y?-1:x>y?1:0)*(sortRev?-1:1);});
  return rows;
}

function renderHead(){
  applyCols();
  document.getElementById('thead').innerHTML = visCols().map(c=>{
    const arr = c.key===sortCol ? `<span class="arr">${sortRev?'▼':'▲'}</span>`:'';
    return `<div class="th ${c.num?'num':''}" onclick="sortBy('${c.key}')">${t(c.label)}${arr}</div>`;
  }).join('');
}

function render(){
  const rows=visible();
  const tb=document.getElementById('tbody');
  if(!STATE.found){
    tb.innerHTML=`<div class="empty"><div><div class="big">${t('Kein Sessions-Ordner gefunden')}</div>
      ${t('Lege ihn unter „Einstellungen“ fest.')}</div></div>`;
    document.getElementById('count').textContent='';
    return;
  }
  // Auswahl loeschen, wenn die Zeile (durch Suche/Filter) nicht mehr sichtbar ist
  if(selected && !rows.some(s=>s.id===selected)) selected=null;
  if(rows.length===0){
    tb.innerHTML=`<div class="empty"><div><div class="big">${t('Keine Sessions')}</div>${t('Nichts gefunden.')}</div></div>`;
  } else {
    tb.innerHTML=rows.map(s=>{
      const col=s.color;
      let style='', cls='row';
      if(col){const tc=lum(col)>150?'#10101a':'#ffffff';
        style=`style="background:${col};color:${tc}"`; cls+=' colored';}
      if(selected===s.id) cls+=' sel';
      const cells=visCols().map(c=>cellHtml(s,c.key)).join('');
      // data-raw: in den Zeilen stehen Sessiontitel und Pfade. Ein Titel, der
      // zufaellig wie ein Oberflaechentext lautet, darf nicht mituebersetzt
      // werden.
      return `<div class="${cls}" ${style} data-id="${s.id}" data-raw
        onclick="selectRow('${s.id}')" ondblclick="doResumeRow('${s.id}')">${cells}</div>`;
    }).join('');
  }
  const total=sessions.length, q=document.getElementById('search').value.trim();
  let txt = q ? t('{n} Treffer', {n: rows.length}) : t('{n} Sessions', {n: rows.length});
  document.getElementById('count').textContent=txt;
  updateDetail();   // Panel/Buttons immer synchron zur Auswahl halten
}

function selectRow(id){ selected = (selected===id ? null : id); render(); }   // erneuter Klick = abwählen
function getSel(){return sessions.find(s=>s.id===selected);}

function updateDetail(){
  const s=getSel();
  const en=!!s;
  // rechtes Panel nur zeigen, wenn etwas ausgewaehlt ist
  document.querySelector('.main').classList.toggle('show-side', en);
  ['btn-resume','btn-rename','btn-color','btn-copy'].forEach(b=>document.getElementById(b).disabled=!en);
  const d=document.getElementById('detail');
  if(!s){
    d.innerHTML=`<div class="dt-empty">${t('Wähle eine Session aus, um Details zu sehen.')}</div>`;
    return;
  }
  const start = (s.first_user||'').replace(/\s+/g,' ').trim().slice(0,600);
  const pfad  = s.cwd || t('(unbekannt)');
  // Nur Anfang und Ende der ID: die vollen 36 Zeichen brachen ueber zwei
  // Zeilen um und standen ganz oben, obwohl man sie fast nie braucht.
  const kurz = s.id.length > 16
    ? s.id.slice(0,8) + '…' + s.id.slice(-4)
    : s.id;
  d.innerHTML =
     `<div class="dt-head" data-raw>${esc(s.display_title || t('(ohne Titel)'))}</div>`
   + `<div class="dt-rows">`
   +   `<div class="k">${t('Ordner')}</div>`
   +   `<div class="v path" data-raw title="${esc(pfad)}">${esc(pfad)}</div>`
   +   `<div class="k">${t('Verlauf')}</div>`
   +   `<div class="v">${t('{du} von dir · {claude} von Claude',
                          {du: s.user_msgs, claude: s.assistant_msgs})}</div>`
   +   `<div class="k">ID</div>`
   +   `<div class="v id" data-raw title="${esc(s.id)}">${esc(kurz)}</div>`
   + `</div>`
   + (start ? `<div class="dt-quote"><span class="k">${t('Erste Frage')}</span>`
            + `<div class="t" data-raw>${esc(start)}</div></div>` : '');
}

function sortBy(c){
  if(sortCol===c) sortRev=!sortRev;
  else {sortCol=c; sortRev=(c==='msgs'||c==='when');}
  api.update_setting('sort_col',sortCol);
  api.update_setting('sort_rev',sortRev);
  renderHead(); render();
}

let BUDDY_STATUS_TIMER = null;
function switchView(v){
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('active',el.dataset.view===v));
  document.getElementById('view-sessions').classList.toggle('active',v==='sessions');
  document.getElementById('view-settings').classList.toggle('active',v==='settings');
  document.getElementById('view-buddy').classList.toggle('active',v==='buddy');
  if(v==='buddy'){
    renderBuddy();
    if(!BUDDY_STATUS_TIMER) BUDDY_STATUS_TIMER = setInterval(refreshBuddyStatus, 2500);
  } else if(BUDDY_STATUS_TIMER){
    clearInterval(BUDDY_STATUS_TIMER); BUDDY_STATUS_TIMER = null;
  }
  renderShortcutBar(v);
  try{ api.buddy_notify_view(v); }catch(_){}
}

// Alles neu zeichnen - gebraucht beim Sprachwechsel. Die Ansichten bauen
// sich ohnehin bei jedem Wechsel neu auf, ein Neustart ist unnoetig.
function renderAll(){
  const tab = document.querySelector('.tab.active');
  const v = tab ? tab.dataset.view : 'sessions';
  renderHead();
  render();
  renderSettings();
  if(v === 'buddy') renderBuddy();
  renderShortcutBar(v);
}

// Tastaturkuerzel je Ansicht. Bewusst nur das, was der keydown-Handler und
// die Maus-Bindungen wirklich koennen - eine Fusszeile, die Kuerzel erfindet,
// ist schlimmer als gar keine. F11 gilt ueberall und steht deshalb ueberall.
const SHORTCUTS = {
  sessions: [['Doppelklick','einsteigen'], ['Enter','einsteigen'],
             ['F2','umbenennen'], ['Rechtsklick','Menü'], ['F11','Vollbild']],
  buddy:    [['Rechtsklick','Buddy kurz wegschicken'],
             ['Doppelklick','dasselbe'], ['Ziehen','verschieben'],
             ['F11','Vollbild']],
  settings: [['Esc','Dialog schließen'], ['F11','Vollbild']],
};
function renderShortcutBar(v){
  const bar = document.getElementById('shortcutbar');
  if(!bar) return;
  const list = SHORTCUTS[v] || [];
  // Beide Haelften uebersetzen: „Doppelklick" ist genauso ein Wort wie das,
  // was danach steht - nur „resume" zu uebersetzen sah halb fertig aus.
  bar.innerHTML = list.map(([k, was])=>
    `<span><kbd>${esc(t(k))}</kbd>${esc(t(was))}</span>`).join('');
}
async function refreshBuddyStatus(){
  try{
    const d = await api.buddy_state();
    if(!d) return;
    const b = d.config || {};
    let s;
    if (!d.have_sprites) s = 'Sprite-Daten fehlen – bitte neu installieren.';
    else if (!b.enabled) s = 'Buddy aus';
    else if (!d.running) s = 'Startet…';
    else if (d.reason) s = t('Buddy läuft · {grund}', {grund: d.reason});
    else s = t('Buddy läuft');
    const el = document.getElementById('buddy-status');
    if(el) el.textContent = s;
  }catch(_){}
}

async function doRefresh(btn){if(btn)btn.disabled=true; ingest(await api.refresh()); render(); updateDetail(); if(btn)btn.disabled=false;}
async function doResume(){const s=getSel(); if(!s)return; await api.resume(s.id,s.cwd,s.project||'');}
async function doResumeRow(id){const s=sessions.find(x=>x.id===id); if(!s)return; selected=id; render(); await api.resume(s.id,s.cwd,s.project||'');}
async function doCopy(){const s=getSel(); if(!s)return; await api.copy(s.id); toast(t('Session-ID kopiert ✓'));}

/* ---- Farbe ---- */
function buildSwatches(){
  const html=COLORS.map(c=>`<div class="sw" style="background:${c}" onclick="setColor('${c}')"></div>`).join('');
  document.getElementById('color-swatches').innerHTML=html;
}
function openColor(){const s=getSel(); if(!s)return;
  const start=s.color||STATE.settings.accent||'#6c6cff';
  const v=hex2hsv(start); CP.h=v.h; CP.s=v.s||1; CP.v=(v.v===undefined?1:v.v); cpRender();
  document.getElementById('overlay-color').classList.add('show');}
async function setColor(c){const s=getSel(); if(!s){closeOverlay('overlay-color');return;}
  ingest(await api.set_color(s.id,c)); render(); updateDetail(); closeOverlay('overlay-color');}

/* eigener Farbwaehler (HSV) */
let CP={h:250,s:1,v:1}, CPdrag=false;
function hsv2hex(h,s,v){h/=360;let i=Math.floor(h*6),f=h*6-i,p=v*(1-s),q=v*(1-f*s),t=v*(1-(1-f)*s),r,g,b;
  switch(i%6){case 0:r=v,g=t,b=p;break;case 1:r=q,g=v,b=p;break;case 2:r=p,g=v,b=t;break;
    case 3:r=p,g=q,b=v;break;case 4:r=t,g=p,b=v;break;default:r=v,g=p,b=q;}
  const z=x=>('0'+Math.round(x*255).toString(16)).slice(-2); return '#'+z(r)+z(g)+z(b);}
function hex2hsv(hex){hex=(hex||'').replace('#',''); if(hex.length===3)hex=hex.split('').map(c=>c+c).join('');
  let r=parseInt(hex.substr(0,2),16)/255,g=parseInt(hex.substr(2,2),16)/255,b=parseInt(hex.substr(4,2),16)/255;
  if(isNaN(r))return{h:250,s:1,v:1};
  let mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn,h=0;
  if(d){if(mx===r)h=((g-b)/d+6)%6;else if(mx===g)h=(b-r)/d+2;else h=(r-g)/d+4;h*=60;}
  return {h:h, s:mx?d/mx:0, v:mx};}
function cpHex(){return hsv2hex(CP.h,CP.s,CP.v);}
function cpRender(){
  const sv=document.getElementById('cp-sv'); if(!sv)return;
  sv.style.background='hsl('+CP.h+',100%,50%)';
  const dot=document.getElementById('cp-sv-dot');
  dot.style.left=(CP.s*100)+'%'; dot.style.top=((1-CP.v)*100)+'%';
  const hex=cpHex();
  document.getElementById('cp-prev').style.background=hex;
  document.getElementById('cp-hex').value=hex;
  document.getElementById('cp-hue').value=CP.h;
}
function cpPick(e){const sv=document.getElementById('cp-sv'),r=sv.getBoundingClientRect();
  CP.s=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
  CP.v=Math.max(0,Math.min(1,1-(e.clientY-r.top)/r.height)); cpRender();}
function cpDown(e){CPdrag=true; try{e.target.setPointerCapture(e.pointerId);}catch(_){} cpPick(e);}
function cpHexIn(v){const x=hex2hsv(v); CP.h=x.h; CP.s=x.s; CP.v=x.v; cpRender();}

/* ---- Umbenennen ---- */
function openRename(){const s=getSel(); if(!s)return;
  const i=document.getElementById('rename-input'); i.value=s.display_title;
  document.getElementById('overlay-rename').classList.add('show'); setTimeout(()=>{i.focus();i.select();},50);}
async function saveRename(){const s=getSel(); if(!s)return;
  ingest(await api.rename(s.id,document.getElementById('rename-input').value)); render(); updateDetail(); closeOverlay('overlay-rename');}
async function resetTitle(){
  const s=getSel(); if(!s) return;
  ingest(await api.rename(s.id,''));   // leerer Titel = Override loeschen -> Auto-Titel
  render(); updateDetail(); closeOverlay('overlay-rename');
  toast(t('Standard-Titel wiederhergestellt'));
}
function closeOverlay(id){document.getElementById(id).classList.remove('show');}

/* ---- Buddy ---- */
let BUDDY=null;   // wird beim ersten Oeffnen geladen
let BUDDY_PREVIEWS={};  // {animName: dataURL} – Cache damit Vorschauen beim Rerender nicht flackern
let BUDDY_MON_CACHE=[]; // Monitor-Liste zwischen Rerenders halten – verhindert Layout-Sprung
async function renderBuddy(){
  const data = await api.buddy_state();
  BUDDY = data;
  const b = data.config || {};
  const anims = data.anims || [];
  const previewName = data.preview_name || 'idle breathe';
  const previewSrc = data.preview || '';
  // Kopf-Vorschau (Miniatur im Titel)
  // Ueberschrift bekommt die freigestellte Fassung, nicht den vollen
  // 20x20-Rahmen - sonst sitzt ein kleiner Klecks in einem schwarzen Quadrat.
  const hp = document.getElementById('buddy-heading-preview');
  if (hp) { try { hp.src = await api.buddy_icon(previewName); } catch(e){} }

  let statusTxt;
  if (!data.have_sprites) statusTxt = 'Sprite-Daten fehlen – bitte neu installieren.';
  else if (!b.enabled) statusTxt = 'Buddy aus';
  else if (!data.running) statusTxt = 'Startet…';
  else if (data.reason) statusTxt = t('Buddy läuft · {grund}', {grund: data.reason});
  else statusTxt = t('Buddy läuft');
  document.getElementById('buddy-status').textContent = statusTxt;

  const size = Math.max(2, Math.min(10, +b.size||4));
  const opacity = Math.max(20, Math.min(100, +b.opacity||100));
  const vis = b.visibility || 'always';
  const target = b.target_window || '';

  // Animations-Vorschau-Grid (Klick = kurz auf dem echten Buddy abspielen)
  const previewList = anims.map(a=>{
    const cached = BUDDY_PREVIEWS[a.name] || '';
    const srcAttr = cached ? `src="${cached}"` : '';
    return `<div class="ba-cell" title="${esc(t('{name} · {n} Frames · Klick zum Vorspielen', {name: a.name, n: a.frames}))}" onclick="buddyPickAnim('${esc(a.name)}', this)">
      <img data-anim="${esc(a.name)}" ${srcAttr} alt="${esc(a.name)}">
      <div class="ba-name">${esc(a.name)}</div>
    </div>`;
  }).join('');

  document.getElementById('buddy-panel').innerHTML=`
    <div class="card">
      <div class="ba-headline">
        <div>
          <h2>${ic('buddy')}Dein kleiner Buddy auf dem Desktop</h2>
          <div class="sub">Ein winziger animierter Clawd (20×20 Pixel) schwebt auf dem Desktop – frameless, immer im Vordergrund. Zieh ihn mit der Maus wohin du magst. Rechts- oder Doppelklick schickt ihn kurz weg – er kommt beim nächsten neuen Claude-Terminal von selbst zurück.</div>
        </div>
        <div class="ba-toggle">
          <div class="toggle ${b.enabled?'on':''}" onclick="buddyToggle()"></div>
          <div class="ba-toggle-lbl">${b.enabled?'An':'Aus'}</div>
        </div>
      </div>
    </div>

    <div class="ba-off-hint ${b.enabled?'':'show'}">
      ${ic('info')}<span>Der Buddy ist ausgeschaltet. Die Einstellungen darunter
      wirken erst, wenn du ihn oben einschaltest.</span>
    </div>

    <div class="ba-sub ${b.enabled?'':'off'}">
    <div class="card">
      <h2>${ic('clock')}Wann sichtbar</h2>
      <div class="sub">Der Buddy kann immer da sein oder nur wenn ein bestimmtes Programm gerade im Vordergrund ist – z.B. nur wenn Claude Code im Terminal läuft.</div>
      <div class="ba-vis">
        <label class="ba-radio"><input type="radio" name="ba-vis" ${vis==='when_claude'?'checked':''} onchange="buddySet('visibility','when_claude')"> <span>Nur wenn Claude Code läuft <em class="ba-dim">(erkennt Terminal + <code>claude.exe</code>)</em></span></label>
        <label class="ba-radio"><input type="radio" name="ba-vis" ${vis==='always'?'checked':''} onchange="buddySet('visibility','always')"> <span>Immer sichtbar</span></label>
        <label class="ba-radio"><input type="radio" name="ba-vis" ${vis==='when_window'?'checked':''} onchange="buddySet('visibility','when_window')"> <span>Nur wenn dieses Fenster vorne ist:</span></label>
      </div>
      <div class="ba-window ${vis==='when_window'?'':'disabled'}">
        <input type="text" id="ba-target" placeholder="z.B. „claude" oder Titel-Ausschnitt" value="${esc(target)}"
               onchange="buddySet('target_window', this.value)">
        <button class="btn" onclick="buddyPickWindow()">Aus offenen Fenstern wählen…</button>
      </div>
      <div class="ba-hint">Passt zu jedem Fenster, dessen Titel den eingegebenen Text enthält (Groß-/Kleinschreibung egal).</div>
    </div>

    <div class="card">
      <h2>${ic('wand')}Aussehen &amp; Position</h2>
      <div class="sub">Größe und Deckkraft ändern sich sofort. Für die Position wähle eine Ecke oder Kante – oder ziehe den Buddy per „Platzieren" frei hin (Bewegung rastet aufs Raster und schnappt am Bildschirmrand).</div>

      <div class="ba-slider">
        <label>Größe <span class="ba-val" id="ba-size-val">${size*20} px</span></label>
        <input type="range" min="2" max="10" step="1" value="${size}" oninput="buddyLive('size', +this.value)" onchange="buddySet('size', +this.value)">
      </div>
      <div class="ba-slider">
        <label>Deckkraft <span class="ba-val" id="ba-op-val">${opacity} %</span></label>
        <input type="range" min="20" max="100" step="5" value="${opacity}" oninput="buddyLive('opacity', +this.value)" onchange="buddySet('opacity', +this.value)">
      </div>

      <div class="ba-frame-row">
        <div class="ba-frame-styles">
          <span class="ba-frame-lbl">Rahmen</span>
          ${[
            ['off','Aus'],
            ['classic','Cam'],
          ].map(([v,l])=>{
            const cur = (b.frame_style==='webcam'?'classic':(b.frame_style||'off'));
            const active = (cur===v)?'active':'';
            return `<button class="ba-style ${active}" onclick="buddySet('frame_style','${v}')">${l}</button>`;
          }).join('')}
        </div>
        <div class="ba-frame-colors ${(!b.frame_style || b.frame_style==='off')?'dim':''}">
          ${['#ec7456','#6c6cff','#3ecf8e','#4aa3ff','#ffb454','#ff6b6b','#c08cff','#34d6c8','#ffffff']
             .map(c=>`<div class="ba-fc ${b.frame_color===c?'active':''}" style="background:${c}" onclick="buddySet('frame_color','${c}')"></div>`).join('')}
        </div>
      </div>
      <div class="ba-frame-label ${(b.frame_style==='classic'||b.frame_style==='webcam')?'':'hidden'}">
        <label>Cam-Name <input type="text" maxlength="7" value="${esc(b.frame_label||'CLAWD')}" onchange="buddySet('frame_label', this.value)"></label>
      </div>

      <div class="ba-anchor-row">
        <div class="ba-anchor-lbl">Schnellwahl</div>
        <div class="ba-monitor-tabs" id="ba-mon-tabs"></div>
        <div class="ba-anchor-grid">
          <button class="ba-anchor" title="Oben links"   onclick="buddyAnchor('tl')"></button>
          <button class="ba-anchor" title="Oben Mitte"   onclick="buddyAnchor('tc')"></button>
          <button class="ba-anchor" title="Oben rechts"  onclick="buddyAnchor('tr')"></button>
          <button class="ba-anchor" title="Mitte links"  onclick="buddyAnchor('ml')"></button>
          <button class="ba-anchor" title="Mitte"        onclick="buddyAnchor('c')"></button>
          <button class="ba-anchor" title="Mitte rechts" onclick="buddyAnchor('mr')"></button>
          <button class="ba-anchor" title="Unten links"  onclick="buddyAnchor('bl')"></button>
          <button class="ba-anchor" title="Unten Mitte"  onclick="buddyAnchor('bc')"></button>
          <button class="ba-anchor" title="Unten rechts" onclick="buddyAnchor('br')"></button>
        </div>
      </div>

      <div class="ba-actions">
        <div class="ba-pos-hint">Aktuell bei <code>${b.x||200}, ${b.y||200}</code></div>
        <button class="btn accent" onclick="buddyPlace()">Buddy platzieren…</button>
      </div>
    </div>

    <div class="card">
      <h2>${ic('play')}Animationen ausprobieren</h2>
      <div class="sub">Normalerweise wählt der Buddy die Animation automatisch nach dem, was in deinen Sessions passiert. Klick eine Animation an, um sie kurz auf dem echten Buddy vorzuspielen.</div>
      <div class="ba-grid">${previewList}</div>
      <div class="ba-actions">
        <button class="btn accent" onclick="buddySurprise()">Kurz „Überraschung" zeigen</button>
        <div class="ba-party">
          <span>Party-Modus (nur Tanz)</span>
          <div class="toggle ${b.party?'on':''}" onclick="buddySetToggle('party')"></div>
        </div>
      </div>
    </div>
    </div>
  `;
  translateDom(document.getElementById('buddy-panel'));

  // BMP-Previews fuer alle Anims nachladen (nur wenn nicht im Cache)
  document.querySelectorAll('#buddy-panel img[data-anim]').forEach(async img=>{
    const n = img.dataset.anim;
    if(img.src) return;   // schon aus Cache befuellt
    const src = await api.buddy_preview(n);
    BUDDY_PREVIEWS[n] = src;
    img.src = src;
  });
  // Monitor-Tabs sofort aus Cache rendern damit kein Layout-Sprung entsteht
  if(BUDDY_MON_CACHE.length){ renderMonitorTabs(BUDDY_MON_CACHE); }
  buddyLoadMonitors();
}
function renderMonitorTabs(mons){
  const box = document.getElementById('ba-mon-tabs');
  if(!box) return;
  if(!mons || !mons.length){ box.innerHTML=''; return; }
  if(BUDDY_MON_IDX!==null && BUDDY_MON_IDX >= mons.length) BUDDY_MON_IDX=null;
  const tabs = mons.map(m=>{
    const active = (BUDDY_MON_IDX===m.idx)?'active':'';
    return `<button class="ba-monitor-tab ${active}" onclick="buddyPickMonitor(${m.idx})">${esc(m.label)}</button>`;
  }).join('');
  const auto = (BUDDY_MON_IDX===null)?'active':'';
  box.innerHTML = `<button class="ba-monitor-tab ${auto}" onclick="buddyPickMonitor(null)" title="${t('Ecke/Kante auf dem Monitor unter dem Buddy')}">aktuell</button>` + tabs;
  translateDom(box);
}

async function buddyToggle(){
  const b = (BUDDY&&BUDDY.config)||{};
  const next = !b.enabled;
  await api.buddy_set('enabled', next);
  await renderBuddy();
  toast(next ? t('Buddy an ✓') : t('Buddy aus'));
}
async function buddySet(key, value){
  await api.buddy_set(key, value);
  renderBuddy();
}
async function buddySetToggle(key){
  const b=(BUDDY&&BUDDY.config)||{};
  await api.buddy_set(key, !b[key]);
  renderBuddy();
}
function buddyLive(key, value){
  const v = document.getElementById(key==='size'?'ba-size-val':'ba-op-val');
  if(v) v.textContent = (key==='size') ? (value*20)+' px' : value+' %';
}
async function buddySurprise(){
  await api.buddy_surprise();
  toast(t('Buddy: Überraschung!'));
}
async function buddyPlace(){
  await api.buddy_place();
}
async function buddyPickAnim(name, cell){
  // Kurz-Feedback im Grid + Buddy spielt die Anim 3.5 s auf dem Desktop
  document.querySelectorAll('#buddy-panel .ba-cell').forEach(el=>el.classList.remove('active'));
  if(cell) cell.classList.add('active');
  setTimeout(()=>{ if(cell) cell.classList.remove('active'); }, 3600);
  const hp = document.getElementById('buddy-heading-preview');
  if(hp){ hp.src = await api.buddy_icon(name); }
  await api.buddy_preview_anim(name);
  toast(t('Buddy zeigt: {name}', {name: name}));
}
let BUDDY_MON_IDX = null;   // Auswahl im Monitor-Picker (null = aktueller unter Buddy)
async function buddyAnchor(pos){
  const st = await api.buddy_anchor(pos, BUDDY_MON_IDX);
  BUDDY = st;
  renderBuddy();
}
async function buddyLoadMonitors(){
  const mons = await api.buddy_monitors();
  BUDDY_MON_CACHE = mons || [];
  renderMonitorTabs(BUDDY_MON_CACHE);
}
function buddyPickMonitor(idx){
  BUDDY_MON_IDX = idx;
  renderMonitorTabs(BUDDY_MON_CACHE);
}
async function buddyPickWindow(){
  const list = await api.buddy_windows();
  if(!list || !list.length){ toast(t('Keine Fenster gefunden.')); return; }
  // Simples Overlay-Menue
  // data-win, nicht data-t: data-t ist fuer Uebersetzungen reserviert, sonst
  // wuerde der Uebersetzungsdurchgang die Fenstertitel ueberschreiben.
  const html = list.map(w=>`<div class="ba-wlist-row" onclick="buddyChoseWindow(this.dataset.win)" data-win="${esc(w)}">${esc(w)}</div>`).join('');
  const box = document.getElementById('overlay-buddy-win');
  box.querySelector('.ba-wlist').innerHTML = html;
  box.classList.add('show');
}
async function buddyChoseWindow(title){
  document.getElementById('ba-target').value = title;
  closeOverlay('overlay-buddy-win');
  await api.buddy_set('target_window', title);
  await api.buddy_set('visibility', 'when_window');
  renderBuddy();
}

/* ---- Einstellungen ---- */
function renderSettings(){
  const st=STATE.settings, found=STATE.found, pdir=STATE.projects_dir||t('(nicht gesetzt)');
  const hidden=st.hidden_folders||[];
  const hl = hidden.length ? hidden.map((f,i)=>`<li>${esc(f)}<span class="x" onclick="unhideIdx(${i})">✕</span></li>`).join('')
    : '<li class="none">Keine</li>';
  const ACCENTS = ['#ec7456','#6c6cff','#3ecf8e','#4aa3ff','#ffb454','#ff6b6b','#c08cff','#34d6c8'];
  const swl = ACCENTS.map(c=>`<div class="sw ${st.accent===c?'active':''}" style="background:${c}" onclick="setAccent('${c}')"></div>`).join('');
  const bgl = BG_TONES.map(tone=>`<div class="sw ${st.bg_base===tone.base?'active':''}" style="background:${shade(tone.base,0.42)}" title="${t(tone.name)}" onclick="setBg('${tone.base}')"></div>`).join('');
  document.getElementById('settings').innerHTML=`
    <div class="secthead" id="sect-auslastung">Auslastung</div>
    <div class="card">
      <h2>${ic('gauge')}Dein Limit</h2>
      <div class="sub">Wie viel vom 5-Stunden-Fenster und von der Woche verbraucht ist. Aktualisiert sich von selbst.</div>
      <div class="limitbox" id="limitbox"><span class="dot off"></span><span class="ltext">…</span></div>
    </div>

    <div class="secthead" id="sect-sessions">Sessions</div>
    <div class="card">
      <h2>${ic('folder')}Sessions-Ordner</h2>
      <div class="sub">Wo Claude die Session-Dateien speichert. Wird automatisch gesucht, lässt sich aber überschreiben.</div>
      <div class="field">
        <input type="text" id="pdir" value="${esc(pdir)}" readonly>
        <span class="badge ${found?'ok':'no'}">${found?'Gefunden':'Nicht gefunden'}</span>
        <button class="btn" onclick="browseFolder()">Durchsuchen…</button>
        <button class="btn" onclick="autoDetect()">Auto</button>
      </div>
    </div>

    <div class="card">
      <h2>${ic('eye')}Anzeige</h2>
      <div class="row2">
        <div><div class="lbl">Heimatordner ausblenden</div>
          <div class="desc">Sessions direkt in ${esc(STATE.home)} verstecken (Unterordner bleiben sichtbar).</div></div>
        <div class="toggle ${st.hide_home?'on':''}" onclick="toggleHome(this)"></div>
      </div>
    </div>

    <div class="card">
      <h2>${ic('hidden')}Weitere ausgeblendete Ordner</h2>
      <div class="sub">Sessions in diesen Ordnern werden komplett ausgeblendet.</div>
      <ul class="hiddenlist">${hl}</ul>
      <button class="btn" onclick="hideCurrent()" style="margin-top:6px">+ Ordner der gewählten Session ausblenden</button>
    </div>

    <div class="card">
      <h2>${ic('columns')}Spalten</h2>
      <div class="sub">Welche Spalten in der Tabelle erscheinen und in welcher Reihenfolge.</div>
      ${normCols().map((c,i,arr)=>`
        <div class="row2">
          <div class="lbl">${ALL_COLS[c.key].label}</div>
          <div style="display:flex; gap:6px; align-items:center">
            <button class="btn mini" onclick="moveCol(${i},-1)" ${i===0?'disabled':''}>▲</button>
            <button class="btn mini" onclick="moveCol(${i},1)" ${i===arr.length-1?'disabled':''}>▼</button>
            <div class="toggle ${c.on?'on':''}" onclick="toggleCol('${c.key}')"></div>
          </div>
        </div>`).join('')}
    </div>

    <div class="secthead" id="sect-darstellung">${t('Darstellung')}</div>
    <div class="card">
      <h2>${ic('globe')}${t('Sprache')}</h2>
      <div class="row2">
        <div><div class="lbl">${t('Sprache der Oberfläche')}</div>
          <div class="desc">${t('„Automatisch" richtet sich nach Windows: deutsche Oberfläche auf deutschen Systemen, sonst Englisch.')}</div></div>
        <select class="sel-input" onchange="setLanguage(this.value)">
          <option value="auto" ${(st.language||'auto')==='auto'?'selected':''}>${t('Automatisch')}</option>
          <option value="de" ${st.language==='de'?'selected':''}>Deutsch</option>
          <option value="en" ${st.language==='en'?'selected':''}>English</option>
        </select>
      </div>
    </div>

    <div class="card">
      <h2>${ic('palette')}Akzentfarbe</h2>
      <div class="sub">Farbe für Buttons, Auswahl und Hervorhebungen.</div>
      <div class="swatches">${swl}</div>
    </div>

    <div class="card">
      <h2>${ic('contrast')}Hintergrund</h2>
      <div class="sub">Grundton der Oberfläche – Flächen, Zeilen und Ränder werden daraus abgeleitet.</div>
      <div class="swatches">${bgl}</div>
    </div>

    <div class="secthead" id="sect-verhalten">Verhalten</div>
    <div class="card">
      <h2>${ic('window')}Fenster schließen</h2>
      <div class="row2">
        <div><div class="lbl">Im Hintergrund weiterlaufen</div>
          <div class="desc">Wenn aktiv, versteckt der X-Button die App nur (Icon im System-Tray unten rechts, Klick öffnet sie wieder).</div>
          ${st.close_to_tray===false ? `<div class="warnnote">${ic('warn')}<span>Das X beendet die App jetzt wirklich – Buddy, Clawdmeter und Benachrichtigungen laufen dann nicht mehr.</span></div>` : ''}</div>
        <div class="toggle ${st.close_to_tray!==false?'on':''}" onclick="toggleTray(this)"></div>
      </div>
      <button class="btn danger" onclick="reallyQuit()" style="margin-top:12px">App jetzt komplett beenden</button>
    </div>

    <div class="card">
      <h2>${ic('power')}Autostart</h2>
      <div class="row2">
        <div><div class="lbl">Mit Windows starten</div>
          <div class="desc">Die App startet automatisch nach dem Anmelden – praktisch damit der Buddy und der Tray-Modus sofort verfügbar sind. Registry-Eintrag unter HKCU\\Run.</div></div>
        <div class="toggle ${st.autostart!==false?'on':''}" onclick="toggleAutostart(this)"></div>
      </div>
    </div>

    <div class="card">
      <h2>${ic('bell')}Benachrichtigungen</h2>
      <div class="row2">
        <div><div class="lbl">Bei Limit-Reset benachrichtigen</div>
          <div class="desc">Windows-Systembenachrichtigung wenn dein Claude-Limit sich zurückgesetzt hat und du wieder loslegen kannst. Braucht den System-Tray aktiv.</div></div>
        <div class="toggle ${st.notify_limit_reset!==false?'on':''}" onclick="toggleLimitNotif(this)"></div>
      </div>
      <div class="row2">
        <div><div class="lbl">Vorwarnen bevor das Limit voll ist</div>
          <div class="desc">Meldet sich einmal pro 5-Stunden-Fenster, sobald die Auslastung die Schwelle erreicht – zusammen mit der Uhrzeit, wann es wieder freigeht.</div>
          ${st.notify_limit_near===false ? `<div class="warnnote">${ic('warn')}<span>Ohne Vorwarnung merkst du es erst bei 100 % – dann ist es zum Reagieren zu spät.</span></div>` : ''}</div>
        <div class="toggle ${st.notify_limit_near!==false?'on':''}" onclick="toggleLimitNear(this)"></div>
      </div>
      <div class="row2">
        <div><div class="lbl">Schwelle für die Vorwarnung</div>
          <div class="desc">Ab wie viel Prozent des 5-Stunden-Limits gewarnt wird.</div></div>
        <div><input type="number" min="10" max="100" step="5"
             value="${st.limit_warn_pct||90}" onchange="setWarnPct(this)"
             style="width:74px;text-align:right"> %</div>
      </div>
    </div>

    <div class="secthead" id="sect-verbindungen">Verbindungen</div>
    <div class="card">
      <h2>${ic('terminal')}Terminal &amp; Claude</h2>
      <div class="row2">
        <div><div class="lbl">Womit öffnen?</div><div class="desc">Wie eine Session gestartet wird.</div></div>
        <select class="sel-input" onchange="api.update_setting('terminal',this.value)">
          <option value="auto" ${st.terminal==='auto'?'selected':''}>Automatisch</option>
          <option value="wt" ${st.terminal==='wt'?'selected':''}>Windows Terminal</option>
          <option value="cmd" ${st.terminal==='cmd'?'selected':''}>Eingabeaufforderung (cmd)</option>
        </select>
      </div>
      <div class="row2">
        <div><div class="lbl">Claude-Befehl</div><div class="desc">Pfad/Name der Claude-CLI (Standard: claude).</div></div>
        <input type="text" style="max-width:260px" value="${esc(st.claude_cmd||'claude')}"
          onchange="api.update_setting('claude_cmd',this.value)">
      </div>
      <div class="row2">
        <div><div class="lbl">Rückfragen zuverlässig erkennen</div>
          <div class="desc">Claude Code meldet dem Buddy selbst, wenn es auf deine Antwort wartet. Ohne das muss die App raten – und rät falsch, sobald mehrere Terminals offen sind: eines arbeitet, das andere fragt. Trägt einen Hook in <code>~/.claude/settings.json</code> ein; deine übrigen Hooks bleiben unangetastet.</div>
          <div class="desc" id="hook-hint"></div></div>
        <div class="toggle" id="hook-toggle" onclick="toggleHooks(this)"></div>
      </div>
    </div>

    <div class="card">
      <h2>${ic('bluetooth')}Clawdmeter</h2>
      <div class="sub">Schickt deine Claude-Auslastung per Bluetooth an ein Clawdmeter-Gerät. Das Gerät muss einmalig in den Windows-Bluetooth-Einstellungen gekoppelt werden.</div>
      <div class="row2">
        <div><div class="lbl">Anbindung aktiv</div><div class="desc" id="clawd-status">…</div></div>
        <div class="toggle ${st.clawdmeter?'on':''}" onclick="toggleClawd(this)"></div>
      </div>
      <div class="row2">
        <div><div class="lbl">Gerät</div><div class="desc">Welches gekoppelte Gerät benutzt wird.</div></div>
        <select class="sel-input" id="clawd-dev" onchange="pickClawd(this.value)">
          <option value="">Wird geladen…</option>
        </select>
      </div>
      <div class="row2">
        <div><div class="lbl">Clawd-Buddy spiegeln</div><div class="desc">Das Gerät zeigt dieselbe Animation wie dein Clawd-Buddy auf dem Desktop — statt selbst eine nach Auslastung zu wählen. Braucht einen eingeschalteten Buddy.</div></div>
        <div class="toggle ${st.clawdmeter_buddy!==false?'on':''}" onclick="toggleClawdBuddy(this)"></div>
      </div>
      <div class="row2">
        <div><div class="lbl">Warnen wenn der Akku zur Neige geht</div>
          <div class="desc">Meldet sich einmal, sobald der Akku des Geräts unter die Schwelle fällt. Erst nach dem Laden wieder.</div></div>
        <div class="toggle ${st.notify_clawd_battery!==false?'on':''}" onclick="toggleClawdBattery(this)"></div>
      </div>
      <div class="row2">
        <div><div class="lbl">Schwelle für die Akku-Warnung</div>
          <div class="desc">Ab wie viel Restladung gewarnt wird.</div></div>
        <div><input type="number" min="5" max="90" step="5"
             value="${st.clawd_battery_pct||15}" onchange="setClawdBatteryPct(this)"
             style="width:74px;text-align:right"> %</div>
      </div>
      <div class="field">
        <button class="btn accent" onclick="clawdReconnect(this)">Jetzt verbinden</button>
        <button class="btn" onclick="loadClawdDevices(true)">Geräte neu suchen</button>
      </div>
    </div>

    <div class="secthead" id="sect-app">App</div>
    <div class="card">
      <h2>${ic('update')}Updates</h2>
      <div class="sub">${t('Aktuelle Version: v{v} — beim Start wird automatisch nach Updates gesucht (ohne Internet wird das übersprungen).', {v: esc(STATE.version||'?')})}</div>
      <div class="field">
        <button class="btn" onclick="manualCheck(this)">Nach Updates suchen</button>
        <span id="upd-status" class="badge"></span>
      </div>
    </div>

    <div class="card">
      <h2>${ic('link')}Projekt</h2>
      <div class="sub">Quelltext, Fehler melden, Änderungen nachlesen.</div>
      <div class="row2">
        <div><div class="lbl">Claude Session Browser</div>
          <div class="desc">Diese App – Quelltext und Releases auf GitHub.</div></div>
        <button class="btn" onclick="api.open_url('https://github.com/juppeee/claude-session-browser')">Öffnen</button>
      </div>
      <div class="row2">
        <div><div class="lbl">Clawdmeter</div>
          <div class="desc">Das Gerät und seine Firmware stammen von Hermann Björgvin. Für Verbrauch und Akku reicht seine Firmware — der Session Browser bringt nur die Anbindung für Windows mit.</div></div>
        <button class="btn" onclick="api.open_url('https://github.com/HermannBjorgvin/Clawdmeter')">Öffnen</button>
      </div>
      <div class="row2">
        <div><div class="lbl">Clawdmeter-Firmware, Fork</div>
          <div class="desc">Hermanns Firmware sucht sich die Animation nach Verbrauchsgeschwindigkeit aus. Damit das Gerät zeigt, was Claude gerade macht, muss dieser Fork darauf laufen — Branch csb-buddy.</div></div>
        <button class="btn" onclick="api.open_url('https://github.com/juppeee/Clawdmeter/tree/csb-buddy')">Öffnen</button>
      </div>
    </div>
  `;
  // Uebersetzen bevor die Sprungleiste gebaut wird - sie liest die
  // Ueberschriften aus dem fertigen Baum.
  translateDom(document.getElementById('settings'));
  buildSettingsJump();
  refreshHooks();
  refreshLimit();
  refreshClawd();
  loadClawdDevices(false);
}

// ---- Limit-Anzeige ----
// LIMIT haelt den zuletzt geholten Stand; der Countdown laeuft daraus jede
// Sekunde weiter, ohne dafuer neu nachzufragen. Frische Zahlen holt
// refreshLimit() im 20-Sekunden-Takt - haeufiger waere sinnlos, die
// Auslastung kommt ohnehin nur aus periodischen API-Abfragen.
let LIMIT = null;
function fmtDauer(sek){
  sek = Math.max(0, Math.round(sek));
  const tage = Math.floor(sek/86400), h = Math.floor(sek/3600), m = Math.floor((sek%3600)/60);
  // Ab einem Tag in Tagen rechnen - "101 h 58 min" muss man erst umrechnen,
  // bevor man weiss, ob das viel ist.
  if(tage > 0) return tage + ' d ' + (h - tage*24) + ' h';
  if(h > 0) return h + ' h ' + String(m).padStart(2,'0') + ' min';
  if(m > 0) return m + ' min ' + String(sek%60).padStart(2,'0') + ' s';
  return sek + ' s';
}
// Eine Kachel: grosse Zahl, Balken, Restzeit - wie auf dem Clawdmeter.
function limitCard(pct, resetAt, tag, voll){
  const rest = resetAt ? (resetAt*1000 - Date.now())/1000 : 0;
  const stufe = voll || pct >= 90 ? 'hot' : (pct >= 60 ? 'mid' : '');
  const sub = voll
    ? (resetAt ? t('voll – zurückgesetzt in {dauer}', {dauer: fmtDauer(rest)}) : t('voll'))
    : (resetAt ? t('zurückgesetzt in {dauer}', {dauer: fmtDauer(rest)})
               : t('Reset-Zeit noch unbekannt'));
  return `<div class="lcard ${stufe}${voll?' full':''}">`
    + `<div class="top"><span class="num">${pct}%</span>`
    +   `<span class="tag">${esc(tag)}</span></div>`
    + `<div class="bar"><i style="width:${Math.max(0,Math.min(100,pct))}%"></i></div>`
    + `<div class="sub">${esc(sub)}</div></div>`;
}
function paintLimit(){
  const box = document.getElementById('limitbox');
  if(!box) return;
  const d = LIMIT;
  if(!d || !d.known){
    box.innerHTML = '<div class="lempty">'
      + t('Noch keine Auslastungsdaten – kommt mit der nächsten Abfrage.')
      + '</div>';
    return;
  }
  if(d.reset_at && d.reset_at*1000 <= Date.now()){ refreshLimit(); return; }
  let html = limitCard(d.hit ? 100 : d.pct, d.reset_at, t('5 Stunden'), d.hit);
  // Wochenwert nur wenn er wirklich vorliegt - ohne Clawdmeter-Abfrage
  // steht er auf 0 und eine leere Kachel waere irrefuehrend.
  if(d.wpct > 0 || d.wreset_at) html += limitCard(d.wpct, d.wreset_at, t('Woche'), false);
  box.innerHTML = html;
}
async function refreshLimit(){
  if(!document.getElementById('limitbox')) return;
  try{ LIMIT = await api.limit_state(); }catch(e){ return; }
  paintLimit();
}
setInterval(()=>{ if(document.getElementById('limitbox')) paintLimit(); }, 1000);
setInterval(()=>{ if(document.getElementById('limitbox')) refreshLimit(); }, 20000);

// ---- Sprungleiste ueber den Einstellungen ----
// Baut sich aus den vorhandenen Sektionsbaendern auf, statt die Namen ein
// zweites Mal zu pflegen: kommt eine Gruppe dazu, steht sie automatisch drin.
function buildSettingsJump(){
  const bar = document.getElementById('settings-jump');
  const box = document.getElementById('settings');
  if(!bar || !box) return;
  const heads = [...box.querySelectorAll('.secthead[id]')];
  if(heads.length < 2){ bar.innerHTML = ''; return; }
  bar.innerHTML = heads.map(h=>
    `<button data-target="${h.id}" onclick="jumpSettings('${h.id}')">${esc(h.textContent)}</button>`
  ).join('');

  // Mitlaufende Hervorhebung: aktiv ist die letzte Ueberschrift, die noch
  // ueber der Oberkante des sichtbaren Bereichs liegt.
  //
  // Die letzten Gruppen erreichen diese Kante nie: unter „App" steht zu wenig
  // Inhalt, um sie nach oben zu schieben - das Scrollen endet vorher am
  // Anschlag. Deshalb gewinnt am unteren Ende immer die letzte Ueberschrift,
  // sonst blieb die Markierung bei „Verhalten" haengen, obwohl man laengst
  // bei „Verbindungen" war.
  const mark = ()=>{
    // Nach einem Klick kurz nicht dazwischenfunken: das weiche Scrollen
    // laeuft noch, und der Nutzer hat sein Ziel ja gerade selbst benannt.
    if(Date.now() < JUMP_LOCK) return;
    let cur;
    if(box.scrollTop + box.clientHeight >= box.scrollHeight - 6){
      cur = heads[heads.length - 1];
    } else {
      const top = box.getBoundingClientRect().top + 12;
      cur = heads[0];
      for(const h of heads){ if(h.getBoundingClientRect().top <= top) cur = h; }
    }
    bar.querySelectorAll('button').forEach(b=>
      b.classList.toggle('active', b.dataset.target === cur.id));
  };
  box.removeEventListener('scroll', box._jumpMark || (()=>{}));
  box._jumpMark = mark;
  box.addEventListener('scroll', mark, {passive:true});
  mark();
}
let JUMP_LOCK = 0;
function jumpSettings(id){
  // Sofort markieren und kurz festhalten: das weiche Scrollen laeuft danach
  // noch, und die letzten Gruppen erreichen den oberen Rand gar nicht mehr -
  // die Markierung waere sonst nie dort angekommen.
  document.querySelectorAll('#settings-jump button').forEach(b=>
    b.classList.toggle('active', b.dataset.target === id));
  JUMP_LOCK = Date.now() + 900;
  const el = document.getElementById(id);
  if(el) el.scrollIntoView({behavior:'smooth', block:'start'});
}

async function loadClawdDevices(rescan){
  const sel = document.getElementById('clawd-dev');
  if(!sel) return;
  if(rescan) sel.innerHTML = '<option value="">' + t('Suche…') + '</option>';
  let r;
  try{ r = await api.clawdmeter_devices(!!rescan); }catch(e){ return; }
  const cur = (STATE.settings.clawdmeter_addr||'');
  const devs = (r&&r.devices)||[];
  const autoName = (devs.find(d=>d.address===(r&&r.auto))||{}).name;
  const autoLbl = r&&r.auto ? `Automatisch (${esc(autoName||r.auto)})` : 'Automatisch (nichts gefunden)';
  let html = `<option value="" ${cur?'':'selected'}>${autoLbl}</option>`;
  if(!devs.length){
    html += '<option value="" disabled>' + t('Keine gekoppelten Bluetooth-Geräte') + '</option>';
  } else {
    html += devs.map(d=>`<option value="${esc(d.address)}" ${cur===d.address?'selected':''}>${esc(d.name)} — ${esc(d.address)}</option>`).join('');
  }
  sel.innerHTML = html;
}
async function pickClawd(addr){
  const r = await api.clawdmeter_pick(addr);
  ingest(await api.get_state());
  setClawdStatus(document.getElementById('clawd-status'), r);
  toast(addr ? t('Gerät gewählt ✓') : t('Gerät wird automatisch gesucht'));
}
// Verbindungszustand als {dot, text}. Der Punkt spart das Lesen - man sieht
// auf einen Blick ob die Verbindung steht, wie beim "Aktuell ✓" der Updates.
function clawdInfo(r){
  if(!r) return {dot:'off', text:''};
  if(!r.available) return {dot:'err', text:t('Bluetooth-Modul nicht verfügbar (bleak fehlt).')};
  if(!r.enabled)   return {dot:'off', text:t('Aus.')};
  const s = r.status || {};
  if(s.connected){
    const ago = s.last_send ? Math.round(Date.now()/1000 - s.last_send) : null;
    // Akku nur zeigen wenn das Geraet ihn meldet - aeltere Firmware tut das
    // nicht, dann steht dort einfach nichts statt "unbekannt".
    // Akku nur wenn das Geraet ihn meldet - aeltere Firmware tut das nicht,
    // dann steht dort einfach nichts statt "unbekannt".
    const akku = (typeof s.battery === 'number') ? s.battery : null;
    return {dot:'ok', akku,
            text: ago===null ? t('Verbunden.')
                             : t('Verbunden — zuletzt gesendet vor {sek}s.', {sek: ago})};
  }
  // Beim Verbinden den Versuch mitzaehlen. Ein stummes "Verbinde…" ueber
  // eine Minute sieht aus wie ein Haenger, obwohl im Hintergrund immer
  // wieder angeklopft wird.
  const nr = s.attempt > 1 ? t(' ({nr}. Versuch)', {nr: s.attempt}) : '';
  return s.last_error ? {dot:'err',  text:t('Nicht verbunden: {grund}', {grund: s.last_error})}
                      : {dot:'wait', text:t('Verbinde…') + nr};
}
function battHtml(pct){
  if(typeof pct !== 'number') return '';
  const stufe = pct <= 15 ? 'low' : (pct <= 40 ? 'mid' : 'ok');
  // Fuellung nie ganz auf 0, sonst sieht die Batterie kaputt statt leer aus.
  const breite = Math.max(2, Math.round(pct * 0.18));   // 18px Innenraum
  return `<span class="batt ${stufe}" title="${esc(t('Akku des Clawdmeter: {pct} %', {pct: pct}))}">`
       +   `<span class="cell"><span class="fill" style="width:${breite}px"></span></span>`
       +   `<span class="pct">${pct} %</span></span>`;
}
function setClawdStatus(el, r){
  if(!el) return;
  const i = clawdInfo(r);
  el.innerHTML = `<span class="dot ${i.dot}"></span>${esc(i.text)}` + battHtml(i.akku);
}
async function refreshClawd(){
  const el = document.getElementById('clawd-status');
  if(!el) return;
  try{ setClawdStatus(el, await api.clawdmeter_state()); }catch(e){}
}
async function toggleClawd(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  const r = await api.clawdmeter_set(on);
  setClawdStatus(document.getElementById('clawd-status'), r);
  toast(on?t('Clawdmeter an ✓'):t('Clawdmeter aus'));
}
async function clawdReconnect(btn){
  const alt = btn.textContent;
  btn.disabled = true; btn.textContent = 'Verbinde…';
  try{
    setClawdStatus(document.getElementById('clawd-status'),
                   await api.clawdmeter_reconnect());
  }catch(e){}
  // Kurz nachfassen: der Versuch laeuft im Hintergrund weiter, der erste
  // Status kommt noch aus der alten Lage.
  setTimeout(refreshClawd, 1500);
  setTimeout(refreshClawd, 5000);
  btn.disabled = false; btn.textContent = alt;
}
async function toggleClawdBuddy(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  ingest(await api.update_setting('clawdmeter_buddy', on));
  toast(on?t('Gerät spiegelt den Clawd-Buddy ✓'):t('Gerät wählt wieder selbst'));
}
setInterval(()=>{ if(document.getElementById('clawd-status')) refreshClawd(); }, 5000);

async function toggleTray(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  ingest(await api.update_setting('close_to_tray', on));
  await api.buddy_apply_tray(on);
  renderSettings();   // der Hinweis darunter haengt am Schalter
}
async function toggleLimitNotif(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  ingest(await api.update_setting('notify_limit_reset', on));
  toast(on?t('Limit-Benachrichtigung an ✓'):t('Limit-Benachrichtigung aus'));
}
async function toggleLimitNear(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  ingest(await api.update_setting('notify_limit_near', on));
  renderSettings();   // der Hinweis darunter haengt am Schalter
  toast(on?t('Vorwarnung an ✓'):t('Vorwarnung aus'));
}
async function setWarnPct(el){
  let v=parseInt(el.value,10); if(isNaN(v)) v=90;
  v=Math.max(10,Math.min(100,v)); el.value=v;
  ingest(await api.update_setting('limit_warn_pct', v));
  toast(t('Warnschwelle: {v}%', {v: v}));
}
async function toggleClawdBattery(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  ingest(await api.update_setting('notify_clawd_battery', on));
}
async function setClawdBatteryPct(el){
  let v=parseInt(el.value,10); if(isNaN(v)) v=15;
  v=Math.max(5,Math.min(90,v)); el.value=v;
  ingest(await api.update_setting('clawd_battery_pct', v));
  // Sperre loesen: nach einer neuen Schwelle soll wieder gewarnt werden
  // duerfen, sonst bliebe eine frueher ausgeloeste Meldung fuer immer stumm.
  await api.update_setting('clawd_battery_warned', false);
  toast(t('Akku-Warnung ab {v}%', {v: v}));
}
async function toggleAutostart(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  const r = await api.set_autostart(on);
  ingest(await api.get_state());
  if(!r || !r.ok) toast(t('Autostart konnte nicht gesetzt werden'));
  else toast(on?t('Autostart an ✓'):t('Autostart aus'));
}
async function reallyQuit(){
  await api.buddy_real_quit();
}
async function browseFolder(){ingest(await api.browse_folder()); render(); renderSettings();}
async function autoDetect(){ingest(await api.update_setting('projects_dir','')); render(); renderSettings();}
async function toggleHome(el){const on=!el.classList.contains('on');
  ingest(await api.update_setting('hide_home',on)); render(); renderSettings();}
async function unhideIdx(i){const f=(STATE.settings.hidden_folders||[])[i]; if(f===undefined)return;
  ingest(await api.remove_hidden_folder(f)); render(); renderSettings();}
async function hideCurrent(){const s=getSel();
  if(!s||!s.cwd){toast(t('Erst im Tab „Sessions" eine Session auswählen.'));return;}
  ingest(await api.add_hidden_folder(s.cwd)); render(); renderSettings(); }
// ---- Hook fuer die Rueckfrage-Erkennung ----
async function refreshHooks(){
  const el = document.getElementById('hook-toggle');
  if(!el) return;
  let r = null;
  try{ r = await api.hooks_state(); }catch(e){ return; }
  el.classList.toggle('on', !!(r && r.on));
  const hint = document.getElementById('hook-hint');
  if(hint) hint.textContent = (r && r.on)
    ? t('Eingerichtet. Neu gestartete Claude-Code-Sitzungen melden sich von selbst.')
    : '';
}
async function toggleHooks(el){
  const an = !el.classList.contains('on');
  el.classList.toggle('on', an);
  let r = null;
  try{ r = await api.hooks_toggle(an); }catch(e){}
  if(!r || !r.ok){ el.classList.toggle('on', !an); toast(t('Hook konnte nicht gesetzt werden')); return; }
  el.classList.toggle('on', !!r.on);
  toast(an ? t('Hook eingerichtet ✓ – gilt ab der nächsten Claude-Code-Sitzung')
           : t('Hook entfernt'));
  refreshHooks();
}

async function setAccent(c){applyAccent(c); ingest(await api.update_setting('accent',c)); renderSettings();}
async function setBg(base){applyBg(base); ingest(await api.update_setting('bg_base',base)); renderSettings();}

async function persistCols(arr){ ingest(await api.update_setting('columns',arr)); renderHead(); render(); renderSettings(); }
function toggleCol(key){ const cols=normCols(); const col=cols.find(c=>c.key===key);
  if(col.on && cols.filter(c=>c.on).length<=1) return;  // mind. eine Spalte sichtbar lassen
  col.on=!col.on; persistCols(cols); }
function moveCol(i,dir){ const cols=normCols(); const j=i+dir; if(j<0||j>=cols.length) return;
  const tmp=cols[i]; cols[i]=cols[j]; cols[j]=tmp; persistCols(cols); }

/* ---- Update ---- */
let UPD=null;
function showUpdateBar(u){
  UPD=u;
  const bar=document.getElementById('updatebar');
  bar.querySelector('.utext').textContent=t('Update verfügbar: v{v}', {v: u.latest});
  bar.querySelector('.unotes').textContent=u.notes? ('— '+u.notes) : '';
  bar.classList.add('show');
}
async function checkUpdate(){
  try{
    // Falls das letzte Update-Batch nicht durchkam, informieren.
    if(await api.consume_update_failed_marker()){
      toast(t('Update konnte nicht übernommen werden – bitte manuell installieren'));
    }
    const u=await api.check_update();
    if(u&&u.available) showUpdateBar(u);
  }catch(_){}
}
function openUpdateDialog(){
  if(!UPD) return;
  document.getElementById('upd-title').textContent=t('Update auf v{neu} (aktuell v{alt})', {neu: UPD.latest, alt: UPD.current});
  document.getElementById('upd-notes').textContent=UPD.notes||t('Verbesserungen und Fehlerbehebungen.');
  const b=document.getElementById('upd-install');
  b.disabled=false; b.textContent= UPD.frozen ? 'Jetzt installieren' : 'Zur Download-Seite';
  document.getElementById('overlay-update').classList.add('show');
}
function buildConfetti(){
  const cols=['#ec7456','#f5926f','#4aa3ff','#3ecf8e','#ffe066','#c08cff','#34d6c8'];
  let h='';
  for(let i=0;i<18;i++){
    const a=(i/18)*6.2832, r=80+(i%3)*26;
    const dx=Math.cos(a)*r, dy=Math.sin(a)*r-18;
    h+=`<i style="--dx:${dx.toFixed(0)}px;--dy:${dy.toFixed(0)}px;background:${cols[i%cols.length]}"></i>`;
  }
  document.getElementById('confetti').innerHTML=h;
}
function setProgress(p){
  p=Math.max(0,Math.min(100, p|0));
  document.getElementById('bar-fill').style.width=p+'%';
  document.getElementById('inst-pct').textContent=p+'%';
}
function startInstallUI(){
  const pop=document.getElementById('upd-pop');
  pop.classList.add('installing'); pop.classList.remove('ready');
  setProgress(0); document.getElementById('inst-state').textContent='Lädt herunter…';
  buildConfetti();
  document.getElementById('overlay-update').classList.add('show');
}
// von Python aufgerufen
window.updateProgress=function(p){
  setProgress(p);
  if(p>=100) document.getElementById('inst-state').textContent='Fast fertig…';
};
window.downloadDone=function(){
  setProgress(100);
  document.getElementById('upd-pop').classList.add('ready');
  document.getElementById('inst-state').textContent='Bereit! Programm startet neu…';
};
async function doInstall(){
  if(!(UPD && UPD.frozen)){   // Dev/keine .exe -> nur Release-Seite oeffnen
    try{ await api.install_update(); }catch(_){}
    closeOverlay('overlay-update'); return;
  }
  startInstallUI();
  let r=null; try{ r=await api.install_update(); }catch(_){}
  if(r && !r.ok){   // Fehler -> zurueck zur Info-Ansicht
    const pop=document.getElementById('upd-pop');
    pop.classList.remove('installing','ready');
    toast(t('Update fehlgeschlagen: {grund}', {grund: (r&&r.error)||t('unbekannt')}));
  }
  // bei Erfolg schliesst Python das Fenster nach der Animation
}
function dismissUpdate(){ document.getElementById('updatebar').classList.remove('show'); }
async function manualCheck(btn){
  btn.disabled=true; const s=document.getElementById('upd-status');
  s.className='badge'; s.textContent=t('Prüfe…');
  let u=null; try{ u=await api.check_update(); }catch(_){}
  if(u && u.available){ showUpdateBar(u); s.className='badge no';
    s.textContent=t('v{v} verfügbar', {v: u.latest});
    openUpdateDialog(); }
  else { s.className='badge ok'; s.textContent=t('Aktuell ✓'); }
  btn.disabled=false;
}

/* ---- Onboarding (erster Start) ---- */
const OB_ACCENTS=['#ec7456','#6c6cff','#3ecf8e','#4aa3ff','#ffb454','#ff6b6b','#c08cff','#34d6c8','#ffe066','#ff8fcf'];
const OB_STEPS=6;
let obStep=0;
function obShow(){
  const returning = !!STATE.settings.onboarded;
  if(returning){
    document.getElementById('ob-title').textContent = t('Neu in dieser Version ✨');
    document.getElementById('ob-intro').innerHTML =
      t('Kurzer Rundgang – deine Einstellungen bleiben unberührt.') + '<br><br>' +
      '<b>' + t('Neu:') + '</b> ' +
      t('Ein animierter Clawd-Buddy für deinen Desktop, der zeigt was Claude gerade macht. Neuer Tab „Buddy" mit allen Einstellungen – Position, Größe, Rahmen, Sichtbarkeit nur wenn Claude Code läuft.');
  }
  const cur=STATE.settings.accent;
  document.getElementById('ob-swatches').innerHTML=OB_ACCENTS.map(c=>
    `<div class="ob-sw ${c===cur?'active':''}" style="background:${c}" onclick="obPickAccent('${c}',this)"></div>`).join('');
  document.getElementById('ob-home').classList.toggle('on', STATE.settings.hide_home!==false);
  const f=document.getElementById('ob-folder');
  f.innerHTML = STATE.found
    ? ('📁 ' + t('Sessions-Ordner gefunden:') + '<br>' + esc(STATE.projects_dir))
    : '⚠️ ' + t('Kein Sessions-Ordner gefunden – du kannst ihn später in den Einstellungen festlegen.');
  obStep=0; obRender();
  document.getElementById('onboard').classList.add('show');
}
function obPickAccent(c,el){
  applyAccent(c); api.update_setting('accent',c);
  document.querySelectorAll('.ob-sw').forEach(s=>s.classList.remove('active'));
  el.classList.add('active');
}
function obToggleHome(el){
  const on=!el.classList.contains('on'); el.classList.toggle('on',on);
  api.update_setting('hide_home',on);
}
function obRender(){
  document.querySelectorAll('.ob-step').forEach(s=>{ s.hidden = (+s.dataset.step!==obStep); });
  let dots=''; for(let i=0;i<OB_STEPS;i++) dots+=`<i class="${i===obStep?'on':''}"></i>`;
  document.getElementById('ob-dots').innerHTML=dots;
  document.getElementById('ob-back').style.visibility = obStep===0?'hidden':'visible';
  document.getElementById('ob-next').textContent = obStep===OB_STEPS-1 ? "Los geht's! 🎉" : 'Weiter';
}
function obNext(){ if(obStep<OB_STEPS-1){ obStep++; obRender(); } else obFinish(); }
function obPrev(){ if(obStep>0){ obStep--; obRender(); } }
async function obFinish(){
  await api.update_setting('onboarded',true);
  ingest(await api.update_setting('onboarded_version', STATE.onboarding_version || ''));
  document.getElementById('onboard').classList.remove('show');
  render(); renderSettings();
}

/* ---- Tastatur ---- */
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){closeOverlay('overlay-color');closeOverlay('overlay-rename');}
  if(e.key==='F11'){ e.preventDefault(); try{api.toggle_fullscreen();}catch(_){}}
  if(e.key==='F2' && getSel()) openRename();
  if(e.key==='Enter'){
    if(document.getElementById('overlay-rename').classList.contains('show')) saveRename();
    else if(getSel() && document.activeElement.id!=='search') doResume();
  }
});
document.getElementById('search').addEventListener('input',render);
document.addEventListener('pointermove',e=>{ if(CPdrag) cpPick(e); });
document.addEventListener('pointerup',()=>{ CPdrag=false; });
document.addEventListener('pointercancel',()=>{ CPdrag=false; });
window.addEventListener('blur',()=>{ CPdrag=false; });

function whenReady(){
  if(window.pywebview && window.pywebview.api && typeof window.pywebview.api.get_state === 'function'){
    api = window.pywebview.api; boot();
  } else {
    setTimeout(whenReady, 80);
  }
}
whenReady();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
#  Selbst-Installation (beim ersten Doppelklick der heruntergeladenen .exe)
# --------------------------------------------------------------------------- #
def _autostart_target_exe():
    """Pfad der zu startenden EXE fuer Autostart (installierte Kopie)."""
    if getattr(sys, "frozen", False):
        # Installierte Version bevorzugen, sonst laufende
        installed = os.path.join(install_dir(), "ClaudeSessionBrowser.exe")
        if os.path.isfile(installed):
            return installed
        return os.path.abspath(sys.executable)
    return None  # Dev-Modus: kein Autostart-Eintrag


def set_autostart(enable):
    """Windows-Autostart via HKCU\\...\\Run. `enable=False` entfernt Eintrag."""
    if not _IS_WIN:
        return False
    try:
        import winreg
    except Exception:
        return False
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    name = "ClaudeSessionBrowser"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_ALL_ACCESS) as k:
            if enable:
                exe = _autostart_target_exe()
                if not exe:
                    return False
                # In Anfuehrungszeichen setzen (Pfad mit Leerzeichen)
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def is_autostart_enabled():
    """Liest den aktuellen Autostart-Status aus der Registry und prueft
    dass die referenzierte .exe wirklich existiert (verwaiste Eintraege
    werden als 'nicht aktiv' behandelt)."""
    if not _IS_WIN:
        return False
    try:
        import winreg
    except Exception:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_READ) as k:
            try:
                v, _ = winreg.QueryValueEx(k, "ClaudeSessionBrowser")
                if not v:
                    return False
                # Pfad extrahieren – v ist typischerweise '"C:\...\exe"'
                path = str(v).strip().strip('"')
                if not os.path.isfile(path):
                    # Verwaister Eintrag – gleich entsorgen
                    try:
                        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                                            0, winreg.KEY_ALL_ACCESS) as k2:
                            winreg.DeleteValue(k2, "ClaudeSessionBrowser")
                    except Exception:
                        pass
                    return False
                return True
            except FileNotFoundError:
                return False
    except Exception:
        return False


def install_dir():
    """Standard-Install-Ort seit v1.1.0: von Inno Setup installiert nach
    %LOCALAPPDATA%\\Programs\\ClaudeSessionBrowser. Wird nur noch von
    _autostart_target_exe() als Fallback benutzt."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    return os.path.join(base, "Programs", "ClaudeSessionBrowser")


def _ps_escape(s):
    """Escaped einen String fuer PowerShell-Single-Quote-Literals.
    In PS werden `'` innerhalb '...' durch '' escaped."""
    return str(s).replace("'", "''")


def _make_shortcuts(target):
    wd = os.path.dirname(target)
    targets = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        targets.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                                    "Programs", "Claude Session Browser.lnk"))
    targets.append(os.path.join(HOME, "Desktop", "Claude Session Browser.lnk"))
    # Pfade escapen – ein `'` in einem Username (z.B. "O'Brien") wuerde sonst
    # aus dem String ausbrechen und PowerShell-Code injizieren.
    t_e = _ps_escape(target)
    wd_e = _ps_escape(wd)
    for lnk in targets:
        lnk_e = _ps_escape(lnk)
        ps = ("$w=New-Object -ComObject WScript.Shell; "
              f"$s=$w.CreateShortcut('{lnk_e}'); $s.TargetPath='{t_e}'; "
              f"$s.WorkingDirectory='{wd_e}'; $s.IconLocation='{t_e},0'; "
              "$s.Save()")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        except OSError:
            pass


def self_install():
    """Deaktiviert seit v1.1.0: Installation laeuft ab jetzt ueber Inno Setup
    Installer (%LOCALAPPDATA%\\Programs\\ClaudeSessionBrowser). Diese alte
    Onefile-Self-Copy-Logik hat bei onedir-Builds die exe ohne _internal-
    Ordner kopiert und die App unstartbar gemacht. Bleibt als No-op stehen
    damit alter Ruf-Code (main() bei aelteren Downloads) nichts kaputt macht."""
    return False


def _migrate_from_selfinstall():
    """v1.1.0 Ein-Mal-Aufraeumung fuer Nutzer die von v1.0.x updaten. Die alte
    Self-Install-Logik hat Shortcuts + Autostart auf
    %LOCALAPPDATA%\\ClaudeSessionBrowser\\ClaudeSessionBrowser.exe gebogen
    (fehlender _internal-Ordner) - bei onedir startet das nicht mehr.
    Diese Funktion putzt das automatisch weg wenn die neue installierte
    App aus dem Inno-Setup-Pfad startet. Idempotent - kann bei jedem Start
    laufen und tut nichts wenn schon sauber."""
    if not getattr(sys, "frozen", False):
        return
    if not _IS_WIN:
        return
    lad = os.environ.get("LOCALAPPDATA") or ""
    if not lad:
        return
    cur = os.path.abspath(sys.executable)
    new_dir = os.path.join(lad, "Programs", "ClaudeSessionBrowser")
    # Nur greifen wenn wir wirklich die installierte v1.1.0 sind
    if os.path.normcase(cur).lower() != os.path.normcase(
            os.path.join(new_dir, "ClaudeSessionBrowser.exe")).lower():
        return
    old_dir = os.path.join(lad, "ClaudeSessionBrowser")
    old_exe = os.path.join(old_dir, "ClaudeSessionBrowser.exe")
    # 1) Verwaisten alten exe/dir loeschen (nie User-Daten - nur exe drin)
    if os.path.isfile(old_exe):
        try:
            os.remove(old_exe)
        except OSError:
            pass
    if os.path.isdir(old_dir):
        try:
            # Nur loeschen wenn wirklich leer (Safety-Net)
            if not os.listdir(old_dir):
                os.rmdir(old_dir)
        except OSError:
            pass

    # 2) Shortcuts umbiegen die auf den alten kaputten Pfad zeigen
    def _mentions_old_path(path):
        """Steht der alte Pfad ueberhaupt in der .lnk?

        Ohne diese Vorpruefung startete die Migration bei JEDEM App-Start
        PowerShell - und jeder dieser Aufrufe blitzt als Konsolenfenster auf.
        Eine .lnk legt Pfade als ASCII und/oder UTF-16 ab, also in beiden
        Kodierungen suchen."""
        try:
            with open(path, "rb") as f:
                data = f.read().lower()
        except OSError:
            return False
        needle = old_dir.lower()
        return (needle.encode("utf-8", "ignore") in data
                or needle.encode("utf-16-le", "ignore") in data)

    def _fix_lnk(path):
        if not os.path.isfile(path):
            return
        if not _mentions_old_path(path):
            return          # zeigt schon woanders hin - nichts zu tun
        try:
            # Nur ueber PowerShell, weil pywin32 nicht ueberall da ist
            ps_target = _ps_escape(cur)
            ps_wd = _ps_escape(new_dir)
            ps_path = _ps_escape(path)
            ps_old = _ps_escape(old_dir)
            script = (
                "$ws=New-Object -ComObject WScript.Shell;"
                f"$s=$ws.CreateShortcut('{ps_path}');"
                f"if($s.TargetPath.ToLower().StartsWith('{ps_old.lower()}')){{"
                f"  $s.TargetPath='{ps_target}';"
                f"  $s.WorkingDirectory='{ps_wd}';"
                f"  $s.IconLocation='{ps_target},0';"
                f"  $s.Save()"
                "}"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           creationflags=0x08000000, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            pass

    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA")
    _fix_lnk(os.path.join(home, "Desktop", "Claude Session Browser.lnk"))
    if appdata:
        _fix_lnk(os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                              "Programs", "Claude Session Browser.lnk"))
        # Und den alten Startup-Ordner-Eintrag komplett weg (Registry uebernimmt)
        old_startup = os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                                   "Programs", "Startup",
                                   "Claude Session Browser.lnk")
        if os.path.isfile(old_startup):
            try:
                os.remove(old_startup)
            except OSError:
                pass

    # 3) Autostart-Registry umbiegen falls sie noch auf den alten Pfad zeigt
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_ALL_ACCESS) as k:
            try:
                v, _ = winreg.QueryValueEx(k, "ClaudeSessionBrowser")
                stripped = str(v).strip().strip('"').lower()
                if stripped.startswith(old_dir.lower()):
                    winreg.SetValueEx(k, "ClaudeSessionBrowser", 0,
                                      winreg.REG_SZ, f'"{cur}"')
            except FileNotFoundError:
                pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
_SINGLE_INSTANCE_MUTEX = "Local\\ClaudeSessionBrowser_SingleInstance_juppeee"
_SINGLE_INSTANCE_LOCKFILE = None  # wird beim Acquire gesetzt


def _acquire_single_instance():
    """Doppelt gesicherter Single-Instance-Guard.

    Ansatz 1: Named-Mutex (`Local\\...`). Windows garantiert dass nur der
    ERSTE Prozess owned=True bekommt. Handle muss gehalten werden bis der
    Prozess zumacht.

    Ansatz 2: Zusaetzlich exclusive Lock-File in `%LOCALAPPDATA%`. Faengt
    Faelle ab wo das Mutex-Handling nicht sauber funktioniert (z.B. Onefile-
    Extract-Race, Antivirus-Injection, alte Windows-Version).

    Rueckgabe: (owned, mutex_handle). owned=False -> zweite Instanz."""
    global _SINGLE_INSTANCE_LOCKFILE
    if not _IS_WIN:
        return True, None

    # Ansatz 1: Named-Mutex
    mutex_says_first = True
    handle = None
    try:
        k = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        # kernel32 mit use_last_error damit GetLastError korrekt lesbar ist
        k.SetLastError(0)
        # CreateMutexW: (SECURITY_ATTRS, bInitialOwner, lpName)
        k.CreateMutexW.restype = ctypes.c_void_p
        handle = k.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
        err = k.GetLastError()
        if handle and err == ERROR_ALREADY_EXISTS:
            mutex_says_first = False
    except Exception:
        # Mutex fehlgeschlagen - verlassen wir uns nur auf Lock-File
        pass

    # Ansatz 2: Lock-File mit exklusiver Sperre
    file_says_first = True
    try:
        lad = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        lock_path = os.path.join(lad, "ClaudeSessionBrowser.instance.lock")
        # Auf Windows brauchen wir msvcrt.locking() fuer exklusive Sperre.
        # Wenn ein anderer Prozess die Datei schon offen und gelockt hat,
        # bekommen wir hier eine OSError.
        import msvcrt
        lf = open(lock_path, "wb")
        try:
            msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
            _SINGLE_INSTANCE_LOCKFILE = lf  # in globals halten -> lock bleibt
        except OSError:
            # Datei gelockt - andere Instanz laeuft
            file_says_first = False
            try:
                lf.close()
            except Exception:
                pass
    except Exception:
        pass

    # Wenn EINER von beiden Wegen sagt "zweite Instanz" -> zweite Instanz.
    # Nur wenn BEIDE sagen "erste Instanz" duerfen wir starten.
    owned = mutex_says_first and file_says_first
    return owned, handle


def _screen_w():
    try:
        if _IS_WIN:
            return int(ctypes.windll.user32.GetSystemMetrics(0))
        elif _IS_MAC:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            w = root.winfo_screenwidth()
            root.destroy()
            return w
        return 1920
    except Exception:
        return 1920


def _screen_h():
    try:
        if _IS_WIN:
            return int(ctypes.windll.user32.GetSystemMetrics(1))
        elif _IS_MAC:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            h = root.winfo_screenheight()
            root.destroy()
            return h
        return 1080
    except Exception:
        return 1080


def _position_is_usable(x, y, w, h):
    """True wenn ein Fenster an (x,y) mit Groesse (w,h) noch greifbar waere.

    Gemerkte Positionen koennen ins Nichts zeigen: ein Monitor wurde
    abgesteckt, die Anordnung hat sich geaendert, oder es steht die
    Minimiert-Position -32000 drin. Das Fenster laege dann ausserhalb jedes
    Bildschirms - in der Taskleiste sichtbar, auf dem Schreibtisch nicht.

    Verlangt wird nicht die volle Flaeche, sondern ein Stueck Titelleiste, das
    man mit der Maus noch treffen kann.
    """
    if not _IS_WIN:
        return True
    try:
        u = ctypes.windll.user32
        vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if vw <= 0 or vh <= 0:
            return True                      # nichts Verlaessliches -> zulassen
        # Ueberlappung von Fenster und Gesamt-Schreibtisch
        ox = min(x + w, vx + vw) - max(x, vx)
        oy = min(y + h, vy + vh) - max(y, vy)
        return ox >= 160 and oy >= 40
    except Exception:
        return True


def _restore_existing_window():
    """Sucht das Hauptfenster der laufenden Instanz und bringt es nach vorne
    (auch aus dem Tray heraus falls verstecked). Return True wenn was gefunden
    und aktiviert wurde."""
    if not _IS_WIN:
        return False
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = {"hwnd": 0}

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def enum_cb(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip().lower()
            if title == _OWN_APP_TITLE_EXACT:
                found["hwnd"] = hwnd
                return False  # stop enum
            return True

        user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
        hwnd = found["hwnd"]
        if not hwnd:
            return False
        # SW_RESTORE=9, SW_SHOW=5. IsIconic pruefen fuer Minimized.
        SW_RESTORE = 9
        SW_SHOW = 5
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        # SetForegroundWindow ist mit Windows-Restrictions oft eingeschraenkt.
        # Trick: kurz Fenster ganz nach oben ziehen und wieder loslassen.
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def main():
    # Hook-Aufruf: nur die Meldung wegschreiben und sofort wieder raus. Kein
    # Fenster, kein Einzelinstanz-Schloss - sonst wuerde jeder Hook-Aufruf
    # mit der laufenden App kollidieren.
    if "--csb-hook" in sys.argv:
        try:
            _hook_entry()
        except Exception:
            pass
        return
    if self_install():
        return  # heruntergeladene Instanz beendet sich; installierte Kopie laeuft
    try:
        _migrate_from_selfinstall()
    except Exception:
        pass
    # Single-Instance-Guard: nur eine App-Instanz gleichzeitig. Weiterer
    # Doppel-Klick bringt die bestehende (evtl. im Tray versteckte) Instanz
    # nach vorne statt eine neue zu starten.
    owned, mutex_handle = _acquire_single_instance()
    if not owned:
        if _restore_existing_window():
            return  # bestehende Instanz wiederhergestellt -> wir sind fertig
        # Kein Fenster findbar - das kann passieren wenn a) eine alte
        # Instanz gerade sauber beendet wurde aber der Mutex/Lock noch
        # kurz gehalten wird, oder b) ein stale-Lock nach einem Crash
        # rumhaengt. Statt still zu enden (der User sieht nichts passieren!)
        # kurz warten und nochmal versuchen. Wenn dann immer noch nicht
        # frei, trotzdem starten - besser als "App reagiert nicht auf
        # Doppelklick" nach einem Update.
        import time as _time
        _time.sleep(1.5)
        owned2, mutex_handle2 = _acquire_single_instance()
        if owned2:
            mutex_handle = mutex_handle2
        else:
            # Immer noch nicht frei UND kein Fenster gefunden ->
            # trotzdem starten. Der Guard hat sich verzockt, aber
            # der User erwartet dass die App aufgeht.
            if _restore_existing_window():
                return
            mutex_handle = mutex_handle2  # womoeglich None
    # Handle in Modul-Scope halten bis Prozess-Ende (Mutex faellt sonst weg)
    globals()["_SINGLE_INSTANCE_HANDLE"] = mutex_handle
    api = Api()
    s = api.settings
    kw = dict(
        html=build_html(), js_api=api, min_size=(820, 520),
        resizable=True, background_color="#14100e",
        width=int(s.get("win_w") or 1180),
        height=int(s.get("win_h") or 760),
        maximized=bool(s.get("win_max")),
    )
    if s.get("win_x") is not None and s.get("win_y") is not None:
        wx, wy = int(s["win_x"]), int(s["win_y"])
        if _position_is_usable(wx, wy, kw["width"], kw["height"]):
            kw["x"], kw["y"] = wx, wy
        else:
            # Unerreichbar gewordene Position wegwerfen statt das Fenster ins
            # Nichts zu setzen. Ohne x/y zentriert pywebview von selbst.
            s["win_x"] = s["win_y"] = None
            s["win_max"] = False
            kw["maximized"] = False
            try:
                save_json(SETTINGS_FILE, s)
            except Exception:
                pass
    win = webview.create_window("Claude Session Browser", **kw)
    api.bind_window(win)

    # Autostart: beim ersten Start eintragen wenn Default aktiv ist.
    # Der Nutzer kann in den Einstellungen abschalten.
    if getattr(sys, "frozen", False):
        want_autostart = bool(s.get("autostart", True))
        already = is_autostart_enabled()
        if want_autostart and not already:
            if set_autostart(True):
                s["autostart_registered"] = True
                save_json(SETTINGS_FILE, s)
        elif not want_autostart and already:
            set_autostart(False)

    # Buddy automatisch anwerfen, wenn er zuletzt an war.
    if s.get("buddy", {}).get("enabled"):
        try:
            api.buddy.start()
        except Exception:
            pass

    # Clawdmeter-Anbindung anwerfen, wenn sie zuletzt an war.
    if s.get("clawdmeter"):
        try:
            link = api._clawd_link()
            if link:
                link.start()
        except Exception:
            pass

    # Limit-Ueberwachung. Laeuft auch ohne Clawdmeter-Hardware, pausiert aber
    # solange der BLE-Link dieselben Header ohnehin schon abfragt.
    if s.get("notify_limit_reset", True) or s.get("notify_limit_near", True):
        try:
            from clawdmeter import UsageWatcher

            def _link_covers():
                link = getattr(api, "_clawdmeter", None)
                return bool(link and link.status().get("connected"))

            api._usage_watcher = UsageWatcher(api.on_usage_meta,
                                              is_covered=_link_covers)
            api._usage_watcher.start()
        except Exception:
            pass

    # System-Tray – aktiv wenn "close_to_tray" gesetzt ist (Default).
    _quit_wanted = {"v": False}

    def real_quit():
        _quit_wanted["v"] = True
        try:
            for w in list(webview.windows):
                w.destroy()
        except Exception:
            pass

    tray = TrayManager(lambda: (webview.windows[0] if webview.windows else None),
                       real_quit)
    if s.get("close_to_tray", True):
        tray.start()

    def on_before_close():
        # Rueckgabewert True erlaubt Schliessen, False verhindert es.
        # WICHTIG: Nur in den Tray verstecken wenn das Tray-Icon auch
        # tatsaechlich laeuft – sonst haette der User keine Moeglichkeit
        # das versteckte Fenster wieder zu holen (Zombie-Prozess).
        if (api.settings.get("close_to_tray", True)
                and not _quit_wanted["v"]
                and tray.icon is not None):
            try:
                win.hide()
            except Exception:
                pass
            return False
        return True

    try:
        win.events.closing += on_before_close
    except Exception:
        pass

    # Fuers UI erreichbar machen: echtes Beenden ueber die App
    api._real_quit = real_quit
    api._tray = tray

    try:
        webview.start()
    finally:
        try:
            api.buddy.stop()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
