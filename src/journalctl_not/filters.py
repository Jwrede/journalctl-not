"""Exclusion rules. An entry is hidden when any rule matches it."""

import fnmatch
import re

from .render import field_bytes

_GLOB_CHARS = set("*?[")


def mangle_unit(name):
    """Mimic `journalctl -u`: a bare name without suffix or glob means NAME.service."""
    if "." not in name and not (_GLOB_CHARS & set(name)):
        return name + ".service"
    return name


def _values(entry, field):
    """All values of a field as str (a field can occur more than once)."""
    value = entry.get(field)
    if value is None:
        return []
    if isinstance(value, list) and value and isinstance(value[0], (list, str)):
        items = value
    else:
        items = [value]
    return [field_bytes(v).decode("utf-8", "surrogateescape") for v in items]


def _any(entry, field, pred):
    return any(pred(v) for v in _values(entry, field))


class FieldRule:
    """FIELD!=VALUE: hide entries where FIELD equals VALUE."""

    def __init__(self, field, value):
        self.field = field
        self.value = value

    def matches(self, entry):
        return _any(entry, self.field, lambda v: v == self.value)

    def __repr__(self):
        return f"{self.field}!={self.value}"


class UnitRule:
    """--exclude-unit: the same fields `journalctl -u` matches on."""

    def __init__(self, pattern):
        self.pattern = mangle_unit(pattern)

    def _m(self, v):
        return fnmatch.fnmatchcase(v, self.pattern)

    def matches(self, e):
        if _any(e, "_SYSTEMD_UNIT", self._m) or _any(e, "COREDUMP_UNIT", self._m):
            return True
        if _values(e, "_PID") == ["1"] and _any(e, "UNIT", self._m):
            return True
        if _values(e, "_UID") == ["0"] and _any(e, "OBJECT_SYSTEMD_UNIT", self._m):
            return True
        return False

    def __repr__(self):
        return f"unit {self.pattern}"


class UserUnitRule(UnitRule):
    """--exclude-user-unit: the user-unit counterparts of the fields above."""

    def matches(self, e):
        return any(
            _any(e, f, self._m)
            for f in ("_SYSTEMD_USER_UNIT", "USER_UNIT", "COREDUMP_USER_UNIT", "OBJECT_SYSTEMD_USER_UNIT")
        )

    def __repr__(self):
        return f"user unit {self.pattern}"


class GrepRule:
    """--exclude-grep: regex on MESSAGE, case-insensitive if the pattern is all lowercase
    (the same "smart case" rule as `journalctl --grep`)."""

    def __init__(self, pattern, case_sensitive=None):
        if case_sensitive is None:
            case_sensitive = pattern != pattern.lower()
        self.regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)

    def matches(self, e):
        return _any(e, "MESSAGE", lambda v: self.regex.search(v) is not None)

    def __repr__(self):
        return f"grep {self.regex.pattern}"


def excluded(entry, rules):
    return any(r.matches(entry) for r in rules)
