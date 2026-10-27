"""journalctl-not: run journalctl and hide entries that match exclusion rules.

All journalctl arguments are passed through. journalctl is run with `-o json`,
entries that match any exclusion are dropped, and the rest is printed in the
requested output mode. -n/--lines counts the entries that remain after the
exclusions, as one would expect.
"""

import json
import os
import re
import subprocess
import sys

from . import __version__
from .filters import FieldRule, GrepRule, UnitRule, UserUnitRule, excluded
from .render import ANSI_HIGHLIGHT, ANSI_NORMAL, TEXT_MODES, render

USAGE = """\
usage: journalctl-not [JOURNALCTL OPTIONS AND MATCHES] [EXCLUSIONS]

Runs journalctl with the given options and matches and hides every entry
that matches one of the exclusions.

exclusions (each may be repeated; an entry is hidden if ANY of them matches):
  FIELD!=VALUE                 hide entries whose FIELD equals VALUE,
                               e.g. _SYSTEMD_UNIT!=cron.service PRIORITY!=7
  --exclude-unit=UNIT          hide a system unit (glob allowed; same fields as -u)
  --exclude-user-unit=UNIT     hide a user unit (glob allowed)
  -T, --exclude-identifier=ID  hide a syslog identifier (like journalctl >= 256)
  --exclude-grep=REGEX         hide entries whose MESSAGE matches REGEX
                               (case-insensitive if REGEX is all lowercase)

journalctl options handled here rather than passed through:
  -f -n -r -e -o --no-pager --no-tail
  supported -o modes: short short-precise short-iso short-iso-precise
  short-full short-unix short-monotonic cat json

environment: JOURNALCTL=path to the journalctl binary (default: journalctl)

examples:
  journalctl-not -f --exclude-unit=noisy.service
  journalctl-not -b -p warning SYSLOG_IDENTIFIER!=kernel --exclude-grep='ACPI'
"""

FIELD_RULE = re.compile(r"^([A-Z0-9_]+)!=(.*)$", re.S)
# journalctl short options that take a required argument / an optional one.
SHORT_REQ = set("DiMopgctuFSU")
SHORT_OPT = set("bn")
JSON_MODES = ("json",)


class UsageError(Exception):
    pass


class Options:
    def __init__(self):
        self.passthrough = []
        self.rules = []
        self.follow = False
        self.lines = None  # None, int N (newest N), ("+", N) oldest N, "all"
        self.reverse = False
        self.pager_end = False
        self.no_pager = False
        self.output = "short"
        self.utc = False
        self.hostname = True
        self.show_all = False
        self.quiet = False
        self.catalog = False


def _parse_lines(value):
    if value == "all":
        return "all"
    if value.startswith("+") and value[1:].isdigit():
        return ("+", int(value[1:]))
    if value.isdigit():
        return int(value)
    raise UsageError(f"invalid --lines value: {value!r}")


def _looks_like_lines(value):
    return value == "all" or value.isdigit() or (value.startswith("+") and value[1:].isdigit())


def parse_args(argv):
    o = Options()
    args = list(argv)
    i = 0

    def take_value(name):
        nonlocal i
        if i + 1 >= len(args):
            raise UsageError(f"option {name} requires an argument")
        i += 1
        return args[i]

    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            raise SystemExit(USAGE)
        if a == "--version":
            raise SystemExit(f"journalctl-not {__version__}")
        m = FIELD_RULE.match(a)
        if m:
            o.rules.append(FieldRule(m.group(1), m.group(2)))
        elif a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in ("--exclude-unit", "--exclude-user-unit", "--exclude-identifier",
                        "--exclude-grep", "--output", "--lines"):
                if not eq:
                    if name == "--lines":
                        val = take_value(name) if i + 1 < len(args) and _looks_like_lines(args[i + 1]) else "10"
                    else:
                        val = take_value(name)
                if name == "--exclude-unit":
                    o.rules.append(UnitRule(val))
                elif name == "--exclude-user-unit":
                    o.rules.append(UserUnitRule(val))
                elif name == "--exclude-identifier":
                    o.rules.append(FieldRule("SYSLOG_IDENTIFIER", val))
                elif name == "--exclude-grep":
                    o.rules.append(GrepRule(val))
                elif name == "--output":
                    o.output = val
                else:
                    o.lines = _parse_lines(val)
            elif a == "--follow":
                o.follow = True
            elif a == "--reverse":
                o.reverse = True
            elif a == "--pager-end":
                o.pager_end = True
            elif a == "--no-pager":
                o.no_pager = True
            elif a == "--no-tail":
                o.lines = "all"
            elif a == "--catalog":
                o.catalog = True
            else:
                if a == "--utc":
                    o.utc = True
                elif a == "--no-hostname":
                    o.hostname = False
                elif a == "--all":
                    o.show_all = True
                elif a == "--quiet":
                    o.quiet = True
                o.passthrough.append(a)
        elif a.startswith("-") and len(a) > 1:
            chars = a[1:]
            keep = ""
            j = 0
            while j < len(chars):
                c = chars[j]
                rest = chars[j + 1:]
                if c == "f":
                    o.follow = True
                elif c == "r":
                    o.reverse = True
                elif c == "e":
                    o.pager_end = True
                elif c == "x":
                    o.catalog = True
                elif c in "aq":
                    if c == "a":
                        o.show_all = True
                    else:
                        o.quiet = True
                    keep += c
                elif c == "n":
                    if rest:
                        o.lines = _parse_lines(rest)
                    elif i + 1 < len(args) and _looks_like_lines(args[i + 1]):
                        o.lines = _parse_lines(take_value("-n"))
                    else:
                        o.lines = 10
                    break
                elif c == "o":
                    o.output = rest or take_value("-o")
                    break
                elif c == "T":
                    o.rules.append(FieldRule("SYSLOG_IDENTIFIER", rest or take_value("-T")))
                    break
                elif c in SHORT_REQ:
                    keep += chars[j:]
                    if not rest:
                        if keep:
                            o.passthrough.append("-" + keep)
                            keep = ""
                        o.passthrough.append(take_value("-" + c))
                    break
                elif c in SHORT_OPT:
                    keep += chars[j:]
                    break
                else:
                    keep += c
                j += 1
            if keep:
                o.passthrough.append("-" + keep)
        else:
            o.passthrough.append(a)
        i += 1

    if o.output not in TEXT_MODES + JSON_MODES:
        raise UsageError(
            f"output mode {o.output!r} is not supported by journalctl-not "
            f"(supported: {', '.join(TEXT_MODES + JSON_MODES)})"
        )
    if o.follow and o.reverse:
        raise UsageError("please specify either --reverse or --follow, not both")
    if o.pager_end and o.lines is None:
        o.lines = 1000
    if o.follow and o.lines is None:
        o.lines = 10
    return o


def _env_bool(name):
    v = os.environ.get(name)
    if v is None:
        return None
    return v.strip().lower() in ("1", "yes", "y", "true", "t", "on")


class Printer:
    """Writes entries in the chosen mode, with journalctl's boot separators."""

    def __init__(self, opts, out, color):
        self.o = opts
        self.out = out
        self.color = color
        self.shown = 0
        self.prev_boot = None

    def entry(self, line, entry):
        o = self.o
        if o.output in JSON_MODES:
            self.out.write(line.rstrip("\n") + "\n")
            self.shown += 1
            return
        text = render(entry, o.output, utc=o.utc, hostname=o.hostname,
                      show_all=o.show_all, color=self.color)
        if text is None:
            return
        boot = entry.get("_BOOT_ID")
        if not o.quiet and self.prev_boot is not None and boot and boot != self.prev_boot:
            sep = f"-- Boot {boot} --"
            self.out.write((ANSI_HIGHLIGHT + sep + ANSI_NORMAL if self.color else sep) + "\n")
        if boot:
            self.prev_boot = boot
        self.out.write(text + "\n")
        self.shown += 1

    def finish(self):
        if self.shown == 0 and not self.o.quiet and not self.o.follow and self.o.output not in JSON_MODES:
            self.out.write("-- No entries --\n")


def _read_entries(proc):
    """Yield (raw line, entry dict) for each JSON line journalctl prints."""
    for line in proc.stdout:
        if not line.startswith("{"):
            continue
        yield line, json.loads(line)


def _spawn(cmd):
    # SYSTEMD_COLORS=true would make journalctl color its JSON output too.
    env = dict(os.environ, SYSTEMD_COLORS="0")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, env=env,
                            encoding="utf-8", errors="surrogateescape", bufsize=1)


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
    proc.stdout.close()
    proc.wait()


def run(opts, out, color):
    journalctl = os.environ.get("JOURNALCTL", "journalctl")
    base = [journalctl] + opts.passthrough + ["--output=json", "--no-pager"]
    if opts.output not in JSON_MODES and not opts.show_all:
        base.append("--all")  # full MESSAGE fields; blobs are shortened by render()
    p = Printer(opts, out, color)
    rules = opts.rules

    if isinstance(opts.lines, int):
        # Newest N entries that survive the exclusions: read backwards until we have N.
        proc = _spawn(base + ["--reverse", "--lines=all"])
        kept, newest_cursor = [], None
        rc = None
        for line, e in _read_entries(proc):
            if newest_cursor is None:
                newest_cursor = e.get("__CURSOR")
            if len(kept) >= opts.lines:
                break
            if not excluded(e, rules):
                kept.append((line, e))
        else:
            rc = proc.wait()
        _stop(proc)
        if rc:
            return rc
        if not opts.reverse:
            kept.reverse()
        for line, e in kept:
            p.entry(line, e)
        out.flush()
        if not opts.follow:
            p.finish()
            return 0
        # Continue after the newest entry seen. Without one nothing matched yet, so
        # following from the start is right (journalctl 255 drops entries with -f -n 0).
        cmd = base + ["--follow", "--lines=all"]
        if newest_cursor:
            cmd.append(f"--after-cursor={newest_cursor}")
    else:
        cmd = list(base)
        if opts.follow:
            cmd.append("--follow")
        cmd.append("--lines=all")
        if opts.reverse:
            cmd.append("--reverse")
    limit = opts.lines[1] if isinstance(opts.lines, tuple) else None

    proc = _spawn(cmd)
    stopped = False
    for line, e in _read_entries(proc):
        if excluded(e, rules):
            continue
        if limit is not None and p.shown >= limit:
            stopped = True
            break
        p.entry(line, e)
        if opts.follow:
            out.flush()
    rc = None if stopped else proc.wait()
    _stop(proc)
    if rc:
        return rc
    p.finish()
    return 0


def _open_pager(opts):
    pager = os.environ.get("SYSTEMD_PAGER", os.environ.get("PAGER", "less"))
    if pager.strip() in ("", "cat"):
        return None
    env = dict(os.environ)
    env.setdefault("LESS", os.environ.get("SYSTEMD_LESS", "FRSXMK"))
    env.setdefault("LESSCHARSET", "utf-8")
    cmd = pager + (" +G" if opts.pager_end and os.path.basename(pager.split()[0]) == "less" else "")
    try:
        return subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="surrogateescape", env=env)
    except OSError:
        return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        opts = parse_args(argv)
    except UsageError as e:
        print(f"journalctl-not: {e}", file=sys.stderr)
        return 1
    if opts.catalog:
        print("journalctl-not: -x/--catalog is not supported and was ignored", file=sys.stderr)

    # MESSAGE can hold invalid UTF-8; write those bytes back out unchanged.
    sys.stdout.reconfigure(errors="surrogateescape")
    tty = sys.stdout.isatty()
    pager = None
    if tty and not opts.no_pager and not opts.follow:
        pager = _open_pager(opts)
    forced = _env_bool("SYSTEMD_COLORS")
    if forced is not None:
        color = forced
    else:
        color = (tty or pager is not None) and "NO_COLOR" not in os.environ \
            and os.environ.get("TERM") != "dumb"

    out = pager.stdin if pager else sys.stdout
    rc = 0
    try:
        rc = run(opts, out, color)
        out.flush()
    except KeyboardInterrupt:
        rc = 130
    except BrokenPipeError:
        rc = 0
    finally:
        if pager:
            try:
                pager.stdin.close()
            except BrokenPipeError:
                pass
            pager.wait()
        else:
            try:
                sys.stdout.flush()
            except BrokenPipeError:
                # Keep Python from complaining again at interpreter exit.
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return rc


if __name__ == "__main__":
    sys.exit(main())
