"""Render journal entries (as read from `journalctl -o json`) like journalctl does.

Only the text formats are implemented: the short-* family and cat. The layout
follows systemd's src/shared/logs-show.c (checked against systemd 255).
"""

import time

SHORT_MODES = (
    "short",
    "short-precise",
    "short-iso",
    "short-iso-precise",
    "short-full",
    "short-unix",
    "short-monotonic",
)
TEXT_MODES = SHORT_MODES + ("cat",)

# Escape sequences from systemd's terminal-util.h.
ANSI_NORMAL = "\x1b[0m"
ANSI_HIGHLIGHT = "\x1b[0;1;39m"
ANSI_HIGHLIGHT_RED = "\x1b[0;1;31m"
ANSI_HIGHLIGHT_YELLOW = "\x1b[0;1;38;5;185m"
ANSI_GREY = "\x1b[0;38;5;245m"
ANSI_BLUE = "\x1b[0;34m"

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_DAYS = "Mon Tue Wed Thu Fri Sat Sun".split()


def field_bytes(value):
    """Journal JSON gives a field as str, as a list of byte values, or as null."""
    if value is None:
        return None
    if isinstance(value, list):
        if value and isinstance(value[0], (list, str)):
            # Field present several times: journalctl shows the first one.
            return field_bytes(value[0])
        return bytes(value)
    return str(value).encode("utf-8", "surrogateescape")


def field_text(entry, name):
    raw = field_bytes(entry.get(name))
    if raw is None:
        return None
    return raw.decode("utf-8", "replace")


def format_bytes(n):
    """systemd's format_bytes(): 9B, 1.0K, 4.9K, 1.2M ..."""
    if n < 1024:
        return f"{n}B"
    for suffix, factor in (("E", 1024**6), ("P", 1024**5), ("T", 1024**4),
                           ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if n >= factor:
            return f"{n // factor}.{(n * 10 // factor) % 10}{suffix}"
    return f"{n}B"


def _unichar_ok(c):
    """systemd's unichar_is_valid() and not unichar_is_control()."""
    ch = ord(c)
    if (ch & 0xFFFFF800) == 0xD800 or 0xFDD0 <= ch <= 0xFDEF or (ch & 0xFFFE) == 0xFFFE:
        return False
    return not ((ch < 32 and c not in "\t\n") or 0x7F <= ch <= 0x9F)


def is_printable(raw):
    """systemd's utf8_is_printable(): valid UTF-8 without control characters."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(_unichar_ok(c) for c in text)


_CSI_CHARS = b"01234567890;m"


def strip_tab_ansi(raw):
    """Port of systemd's strip_tab_ansi() (src/basic/string-util.c, v255).

    Replaces TABs by 8 spaces, drops SGR color sequences (ESC [ ... m) and OSC
    sequences (ESC ] ... BEL, e.g. hyperlinks) and carriage returns before a
    newline or at the end. Everything else is left as it is.
    """
    out = bytearray()
    n = len(raw)
    i = 0
    state = "other"
    begin = 0
    crs = 0
    while i <= n:
        b = raw[i] if i < n else None
        if state == "other":
            if b is None:
                break
            if b == 0x0D:
                crs += 1
                i += 1
                continue
            if b == 0x0A:
                crs = 0
            out += b"\r" * crs
            crs = 0
            if b == 0x1B:
                state = "escape"
            elif b == 0x09:
                out += b" " * 8
            else:
                out.append(b)
        elif state == "escape":
            if b is None:
                out.append(0x1B)
                break
            if b == ord("["):
                state, begin = "csi", i + 1
            elif b == ord("]"):
                state, begin = "cso", i + 1
            else:
                out += bytes([0x1B, b])
                state = "other"
        elif state == "csi":
            if b is None or b not in _CSI_CHARS:
                out += b"\x1b["
                state = "other"
                i = begin
                continue
            if b == ord("m"):
                state = "other"
        else:  # cso
            if b is None or (b != 0x07 and b < 32) or b > 126:
                out += b"\x1b]"
                state = "other"
                i = begin
                continue
            if b == 0x07:
                state = "other"
        i += 1
    return bytes(out)


def _fmt_time(usec, mode, utc):
    sec, frac = divmod(usec, 1_000_000)
    tm = time.gmtime(sec) if utc else time.localtime(sec)
    if mode == "short-unix":
        return f"{sec}.{frac:06d}"
    if mode in ("short-iso", "short-iso-precise"):
        off = tm.tm_gmtoff or 0
        sign = "-" if off < 0 else "+"
        off = abs(off)
        zone = f"{sign}{off // 3600:02d}:{off % 3600 // 60:02d}"
        base = time.strftime("%Y-%m-%dT%H:%M:%S", tm)
        if mode == "short-iso-precise":
            base += f".{frac:06d}"
        return base + zone
    if mode == "short-full":
        zone = "UTC" if utc else time.strftime("%Z", tm)
        return f"{_DAYS[tm.tm_wday]} {time.strftime('%Y-%m-%d %H:%M:%S', tm)} {zone}"
    base = f"{_MONTHS[tm.tm_mon - 1]} {tm.tm_mday:02d} {time.strftime('%H:%M:%S', tm)}"
    if mode == "short-precise":
        base += f".{frac:06d}"
    return base


def _timestamp(entry, mode, utc):
    if mode == "short-monotonic":
        usec = int(entry.get("__MONOTONIC_TIMESTAMP") or 0)
        sec, frac = divmod(usec, 1_000_000)
        return f"[{sec:5d}.{frac:06d}]"
    usec = entry.get("_SOURCE_REALTIME_TIMESTAMP") or entry.get("__REALTIME_TIMESTAMP")
    return _fmt_time(int(usec), mode, utc)


def _first(*values):
    return next((v for v in values if v is not None), None)


def _decode(raw):
    return raw.decode("utf-8", "surrogateescape")


def _colors(entry):
    """(on, off, highlight) as chosen by systemd's get_log_colors(), plus the
    blue that print_multiline() uses for audit messages."""
    prio = field_text(entry, "PRIORITY") or ""
    p = int(prio) if len(prio) == 1 and prio in "01234567" else 6
    if p <= 3:
        on, off, hl = ANSI_HIGHLIGHT_RED, ANSI_NORMAL, ANSI_HIGHLIGHT
    elif p == 4:
        on, off, hl = ANSI_HIGHLIGHT_YELLOW, ANSI_NORMAL, ANSI_HIGHLIGHT
    elif p == 5:
        on, off, hl = ANSI_HIGHLIGHT, ANSI_NORMAL, ANSI_HIGHLIGHT_RED
    elif p == 7:
        on, off, hl = ANSI_GREY, ANSI_NORMAL, ANSI_HIGHLIGHT_RED
    else:
        on, off, hl = "", "", ""
    return on, off, hl


def render(entry, mode="short", *, utc=False, hostname=True, show_all=False, color=False):
    """Return the text journalctl prints for one entry, without trailing newline.

    Returns None when journalctl would print nothing for the entry (no MESSAGE).
    """
    raw = field_bytes(entry.get("MESSAGE"))
    if raw is None:
        return None
    on, off, hl = _colors(entry) if color else ("", "", "")

    if mode == "cat":
        # cat prints MESSAGE as it is: no blob placeholder, tabs and newlines kept.
        text = _decode(raw)
        return on + hl + on + text + off if color else text

    if not show_all:
        raw = strip_tab_ansi(raw)
    if color and not on and field_text(entry, "_TRANSPORT") == "audit":
        on, off = ANSI_BLUE, ANSI_NORMAL

    def shown(name):
        # shall_print(): a field is skipped when it is unprintable or 300+ bytes long
        raw_field = field_bytes(entry.get(name))
        if raw_field is None:
            return None
        if not show_all and (len(raw_field) >= 300 or not is_printable(raw_field)):
            return None
        return _decode(raw_field)

    parts = [_timestamp(entry, mode, utc)]
    host = shown("_HOSTNAME") if hostname else None
    if host is not None:
        parts.append(host)
    ident = _first(shown("SYSLOG_IDENTIFIER"), shown("_COMM"), "unknown")
    pid = _first(shown("_PID"), shown("SYSLOG_PID"))
    parts.append(f"{ident}[{pid}]:" if pid is not None else f"{ident}:")
    prefix = " ".join(parts) + " "

    if not show_all and not is_printable(raw):
        return prefix + f"[{format_bytes(len(raw))} blob data]"
    if not raw:
        return prefix

    # print_multiline(): one output line per "\n"; a single trailing "\n" ends the
    # last line instead of starting an empty one.
    text = _decode(raw)
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    out = []
    # journalctl indents continuation lines by the byte length of the prefix
    indent = " " * len(prefix.encode("utf-8", "surrogateescape"))
    for n, line in enumerate(lines):
        lead = prefix if n == 0 else indent
        if n == 0 and line and color:
            # journalctl passes an empty highlight range at offset 0 for the first line
            out.append(lead + on + hl + on + line + off)
        else:
            out.append(lead + on + line + off)
    return "\n".join(out)
