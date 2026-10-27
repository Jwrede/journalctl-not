import io
import json
import os
import sys
from pathlib import Path

import time

import pytest

from journalctl_not.cli import UsageError, parse_args, run
from journalctl_not.filters import FieldRule, GrepRule, UnitRule, UserUnitRule, excluded
from journalctl_not.render import format_bytes, render, strip_tab_ansi

HERE = Path(__file__).parent
BOOT_A = "a" * 32
BOOT_B = "b" * 32


def set_tz(monkeypatch, name):
    monkeypatch.setenv("TZ", name)
    time.tzset()


@pytest.fixture(autouse=True)
def _restore_tz():
    yield
    time.tzset()


def entry(msg, i=0, **fields):
    e = {
        "__CURSOR": f"c{i}",
        "__REALTIME_TIMESTAMP": str(1_793_078_694_000_000 + i * 1_000_000),
        "__MONOTONIC_TIMESTAMP": str(166_288_000_000 + i),
        "_BOOT_ID": BOOT_A,
        "_HOSTNAME": "host",
        "SYSLOG_IDENTIFIER": "app",
        "_PID": "42",
        "PRIORITY": "6",
        "MESSAGE": msg,
    }
    e.update(fields)
    return e


# --- argument parsing -------------------------------------------------------

def test_parse_passthrough_and_exclusions():
    o = parse_args(["-b", "-1", "-p", "warning", "_SYSTEMD_UNIT!=cron.service",
                    "--exclude-unit", "noisy", "-T", "kernel", "--exclude-grep=ACPI"])
    assert o.passthrough == ["-b", "-1", "-p", "warning"]
    assert [repr(r) for r in o.rules] == [
        "_SYSTEMD_UNIT!=cron.service", "unit noisy.service",
        "SYSLOG_IDENTIFIER!=kernel", "grep ACPI"]


def test_parse_short_clusters():
    o = parse_args(["-xe"])
    assert o.catalog and o.pager_end and o.lines == 1000 and o.passthrough == []
    o = parse_args(["-fu", "nginx"])
    assert o.follow and o.lines == 10 and o.passthrough == ["-u", "nginx"]
    o = parse_args(["-unginx", "-qn20"])
    assert o.passthrough == ["-unginx", "-q"] and o.quiet and o.lines == 20
    o = parse_args(["-n", "5", "-ocat"])
    assert o.lines == 5 and o.output == "cat"
    o = parse_args(["-n", "-k"])
    assert o.lines == 10 and o.passthrough == ["-k"]


def test_parse_lines_forms():
    assert parse_args(["--lines=+3"]).lines == ("+", 3)
    assert parse_args(["--lines", "all"]).lines == "all"
    assert parse_args(["--no-tail", "-f"]).lines == "all"


def test_parse_errors():
    with pytest.raises(UsageError):
        parse_args(["-o", "verbose"])
    with pytest.raises(UsageError):
        parse_args(["-f", "-r"])
    with pytest.raises(UsageError):
        parse_args(["--lines=x"])


# --- exclusion rules ---------------------------------------------------------

def test_unit_rule_matches_journalctl_u_fields():
    r = UnitRule("noisy")
    assert r.matches({"_SYSTEMD_UNIT": "noisy.service"})
    assert r.matches({"_PID": "1", "UNIT": "noisy.service"})
    assert not r.matches({"_PID": "77", "UNIT": "noisy.service"})
    assert r.matches({"_UID": "0", "OBJECT_SYSTEMD_UNIT": "noisy.service"})
    assert r.matches({"COREDUMP_UNIT": "noisy.service"})
    assert not r.matches({"_SYSTEMD_UNIT": "other.service"})
    assert UnitRule("docker-*.scope").matches({"_SYSTEMD_UNIT": "docker-1a2b.scope"})


def test_user_unit_rule():
    r = UserUnitRule("pipewire")
    assert r.matches({"_SYSTEMD_USER_UNIT": "pipewire.service"})
    assert r.matches({"USER_UNIT": "pipewire.service"})
    assert not r.matches({"_SYSTEMD_UNIT": "pipewire.service"})


def test_grep_rule_smart_case():
    assert GrepRule("acpi").matches({"MESSAGE": "ACPI Error"})
    assert not GrepRule("Acpi").matches({"MESSAGE": "ACPI Error"})
    assert GrepRule("^ACPI").matches({"MESSAGE": "ACPI Error"})


def test_field_rule_repeated_field_and_bytes():
    assert FieldRule("TAG", "b").matches({"TAG": ["a", "b"]})
    assert FieldRule("MESSAGE", "hi").matches({"MESSAGE": [104, 105]})
    assert not FieldRule("MESSAGE", "hi").matches({})
    assert excluded({"PRIORITY": "7"}, [FieldRule("X", "1"), FieldRule("PRIORITY", "7")])


# --- rendering ---------------------------------------------------------------

def test_render_short_multiline(monkeypatch):
    set_tz(monkeypatch, "UTC")
    e = entry("multi\nline\n")
    assert render(e) == ("Oct 27 05:24:54 host app[42]: multi\n"
                         "                              line")


def test_render_identifier_fallbacks(monkeypatch):
    set_tz(monkeypatch, "UTC")
    e = entry("x", SYSLOG_IDENTIFIER=None, _COMM="python3", _PID=None, SYSLOG_PID="7")
    assert render(e).endswith("python3[7]: x")
    k = entry("oops", SYSLOG_IDENTIFIER="kernel", _PID=None, _TRANSPORT="kernel")
    assert render(k, hostname=False) == "Oct 27 05:24:54 kernel: oops"
    # early boot messages that systemd tools wrote to /dev/kmsg keep their sender
    kmsg = entry("x", SYSLOG_IDENTIFIER="systemd-fstab-generator", _PID="1578", _TRANSPORT="kernel")
    assert render(kmsg).endswith(" systemd-fstab-generator[1578]: x")
    nothing = entry("x", SYSLOG_IDENTIFIER=None, _PID=None, _HOSTNAME=None)
    assert render(nothing) == "Oct 27 05:24:54 unknown: x"
    unprintable = entry("x", SYSLOG_IDENTIFIER=[97, 1], _COMM="comm")
    assert render(unprintable).endswith(" comm[42]: x")


def test_render_indent_counts_bytes(monkeypatch):
    set_tz(monkeypatch, "UTC")
    out = render(entry("a\nb", SYSLOG_IDENTIFIER="\u00fc"), hostname=False)
    assert out.split("\n")[1] == " " * len("Oct 27 05:24:54 \u00fc[42]: ".encode()) + "b"


def test_render_timestamp_modes(monkeypatch):
    set_tz(monkeypatch, "Europe/Berlin")
    e = entry("x", _SOURCE_REALTIME_TIMESTAMP="1793078694512788")
    assert render(e, "short-precise").startswith("Oct 27 06:24:54.512788 ")
    assert render(e, "short-iso").startswith("2026-10-27T06:24:54+01:00 ")
    assert render(e, "short-iso", utc=True).startswith("2026-10-27T05:24:54+00:00 ")
    assert render(e, "short-unix").startswith("1793078694.512788 ")
    assert render(e, "short-full").startswith("Tue 2026-10-27 06:24:54 CET ")
    assert render(e, "short-monotonic").startswith("[166288.000000] ")


def test_render_blob_and_ansi():
    assert render(entry([99, 1, 100]), hostname=False).endswith("app[42]: [3B blob data]")
    assert render(entry([99, 1, 100]), show_all=True).endswith("app[42]: c\x01d")
    link = "\x1b]8;;https://example.com\x07text\x1b]8;;\x07"
    assert render(entry(link)).endswith(": text")
    assert render(entry("\x1b[1;31mred\x1b[0m\ttab\r\n")).endswith(": red        tab")
    # cursor movement is not stripped, so the message is a blob, as in journalctl
    assert render(entry("\x1b[12Aup")).endswith(": [7B blob data]")


def test_strip_tab_ansi_edge_cases():
    assert strip_tab_ansi(b"a\x1b") == b"a\x1b"
    assert strip_tab_ansi(b"a\x1b[3") == b"a\x1b[3"
    assert strip_tab_ansi(b"a\r\rb\r") == b"a\r\rb"
    assert strip_tab_ansi(b"\x1bXy") == b"\x1bXy"


def test_render_cat_and_empty():
    assert render(entry("as\tis\n"), "cat") == "as\tis\n"
    assert render(entry(""), hostname=False).endswith("app[42]: ")
    assert render(entry(None)) is None


def test_render_colors():
    e = entry("boom\nmore", PRIORITY="3")
    red, hl, off = "\x1b[0;1;31m", "\x1b[0;1;39m", "\x1b[0m"
    out = render(e, color=True, hostname=False).split("\n")
    assert out[0].endswith(f"app[42]: {red}{hl}{red}boom{off}")
    assert out[1].endswith(f"{red}more{off}")
    assert render(entry("plain"), color=True).endswith(": plain")
    # audit messages without a priority color are blue
    assert render(entry("audit", _TRANSPORT="audit"), color=True).endswith(
        ": \x1b[0;34m\x1b[0;34maudit\x1b[0m")


def test_format_bytes():
    assert [format_bytes(n) for n in (9, 1023, 1024, 5000, 3 * 1024**2)] == \
        ["9B", "1023B", "1.0K", "4.8K", "3.0M"]


# --- running against a fake journalctl --------------------------------------

@pytest.fixture
def fake(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture.jsonl"
    arglog = tmp_path / "args.jsonl"
    monkeypatch.setenv("JOURNALCTL", str(HERE / "fake_journalctl.py"))
    monkeypatch.setenv("JNOT_FIXTURE", str(fixture))
    monkeypatch.setenv("JNOT_ARGLOG", str(arglog))
    set_tz(monkeypatch, "UTC")

    def go(entries, argv):
        fixture.write_text("".join(json.dumps(e) + "\n" for e in entries))
        arglog.write_text("")
        out = io.StringIO()
        rc = run(parse_args(argv), out, color=False)
        calls = [json.loads(line) for line in arglog.read_text().splitlines()]
        return rc, out.getvalue(), calls

    return go


ENTRIES = [
    entry("one", 0),
    entry("noise", 1, SYSLOG_IDENTIFIER="noisy"),
    entry("two", 2),
    entry("noise", 3, SYSLOG_IDENTIFIER="noisy"),
    entry("three", 4, _BOOT_ID=BOOT_B),
    entry("noise", 5, SYSLOG_IDENTIFIER="noisy"),
]


def messages(out):
    return [line.split(": ", 1)[1] if ": " in line else line for line in out.splitlines()]


def test_run_filters_everything(fake):
    rc, out, calls = fake(ENTRIES, ["-T", "noisy", "-u", "x"])
    assert rc == 0
    assert messages(out) == ["one", "two", f"-- Boot {BOOT_B} --", "three"]
    assert calls[0][:2] == ["-u", "x"] and "--output=json" in calls[0] and "--all" in calls[0]


def test_run_lines_counts_kept_entries(fake):
    rc, out, calls = fake(ENTRIES, ["-n", "2", "-q", "-T", "noisy"])
    assert messages(out) == ["two", "three"]
    assert "--reverse" in calls[0]


def test_run_lines_reverse_and_oldest(fake):
    _, out, _ = fake(ENTRIES, ["-r", "-n", "2", "-q", "SYSLOG_IDENTIFIER!=noisy"])
    assert messages(out) == ["three", "two"]
    _, out, _ = fake(ENTRIES, ["--lines=+2", "-T", "noisy"])
    assert messages(out) == ["one", "two"]


def test_run_follow_continues_after_newest_cursor(fake):
    _, out, calls = fake(ENTRIES, ["-f", "-n", "1", "-q", "-T", "noisy"])
    assert len(calls) == 2
    assert "--reverse" in calls[0]
    assert "--follow" in calls[1] and "--after-cursor=c5" in calls[1]
    assert messages(out) == ["three"]


def test_run_follow_without_matches_follows_from_start(fake):
    _, out, calls = fake([], ["-f"])
    assert "--follow" in calls[1] and "--lines=all" in calls[1]
    assert not any(a.startswith("--after-cursor") for a in calls[1])
    assert out == ""


def test_run_no_entries_and_json(fake):
    _, out, _ = fake(ENTRIES, ["-T", "noisy", "-T", "app"])
    assert out == "-- No entries --\n"
    _, out, calls = fake(ENTRIES, ["-o", "json", "-T", "noisy"])
    assert [json.loads(line)["MESSAGE"] for line in out.splitlines()] == ["one", "two", "three"]
    assert "--all" not in calls[0]


def test_run_passes_journalctl_errors_through(fake):
    rc, out, _ = fake(ENTRIES, ["--badopt"])
    assert rc == 1 and out == ""


def test_cli_entry_point(tmp_path):
    import subprocess
    r = subprocess.run([sys.executable, "-m", "journalctl_not.cli", "--help"],
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=str(HERE.parent / "src")))
    assert "FIELD!=VALUE" in (r.stdout + r.stderr)
