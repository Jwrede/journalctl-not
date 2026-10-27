# journalctl-not

`journalctl` with NOT matches. Hide a noisy unit, identifier, field value or
message pattern and keep the normal journalctl output: same format, colors,
pager, `-f`, `-n`, `-b`, `-p`, `-u`, `--since` and so on.

```sh
journalctl-not -f --exclude-unit=noisy.service
journalctl-not -b -p warning --exclude-grep='ACPI|firmware bug' _TRANSPORT!=audit
journalctl-not -n 50 -u nginx PRIORITY!=7
```

## Why

journalctl can combine matches with AND and OR, but it has no NOT:

- [systemd/systemd#2720](https://github.com/systemd/systemd/issues/2720)
  "RFE: add NOT operator to journal matching", open since 2016 with 131
  reactions. The maintainer's answer was that NOT is on the TODO list but hard
  to support with the journal indexes; nobody has implemented it since. The
  request goes back to [freedesktop bug 55868](https://bugs.freedesktop.org/show_bug.cgi?id=55868) (2012).
- [How to exclude journalctl entries using a filter?](https://superuser.com/questions/1198464/how-to-exclude-journalctl-entries-using-a-filter)
  on Super User (score 26, about 39k views), and similar questions such as
  [this one](https://unix.stackexchange.com/questions/797873/how-can-i-exclude-specific-syslog-identifier-with-journalctl-filtering)
  on Unix & Linux.
- The workarounds in those threads are `journalctl | grep -v`, which loses the
  colors and the pager unless you set `SYSTEMD_COLORS`, matches inside
  multi-line messages, and makes `-n 20` show fewer than 20 lines, or
  `journalctl -o json | jq 'select(...)'`, which works but prints raw JSON.
- systemd 256 added `-T/--exclude-identifier`, which covers syslog identifiers
  only. Ubuntu 24.04 (systemd 255), Debian 12 (252) and RHEL 9 (252) do not
  have it.

## How it works

`journalctl-not` runs your journalctl command with `-o json`, drops every entry
that matches one of the exclusions and prints the rest the way journalctl
would. The text output is a port of systemd's own formatting code
(`logs-show.c`, `strip_tab_ansi()`), so the result is the same as journalctl's,
byte for byte, for the supported output modes. `tests/golden.sh` checks this
against the real journalctl on the machine it runs on, and CI runs it on Ubuntu
24.04 (systemd 255).

`-n N` counts the entries that remain after the exclusions: journalctl-not reads
the journal backwards until it has N of them. With `-f` it prints those and then
follows from the newest entry.

## Installation

Requires Python 3.9 or newer and journalctl. No other dependencies.

```sh
pipx install git+https://github.com/Jwrede/journalctl-not.git
# or
uv tool install git+https://github.com/Jwrede/journalctl-not.git
```

Reading the system journal needs the same rights as journalctl: be root or in
the `systemd-journal` or `adm` group, or use `sudo journalctl-not ...`.

## Usage

Everything journalctl understands is passed through. These arguments are
handled by journalctl-not:

| Exclusion | Hides entries where |
|---|---|
| `FIELD!=VALUE` | the field equals the value, e.g. `_SYSTEMD_UNIT!=cron.service`, `PRIORITY!=7`, `_TRANSPORT!=kernel` |
| `--exclude-unit=UNIT` | the system unit matches, using the same fields as `journalctl -u` (`_SYSTEMD_UNIT`, `UNIT` from PID 1, `OBJECT_SYSTEMD_UNIT` from root, `COREDUMP_UNIT`). Globs work, a bare name gets `.service` |
| `--exclude-user-unit=UNIT` | `_SYSTEMD_USER_UNIT`, `USER_UNIT`, `COREDUMP_USER_UNIT` or `OBJECT_SYSTEMD_USER_UNIT` matches |
| `-T`, `--exclude-identifier=ID` | `SYSLOG_IDENTIFIER` equals ID (as in systemd 256+) |
| `--exclude-grep=REGEX` | `MESSAGE` matches the Python regular expression; case-insensitive when REGEX is all lowercase, like `journalctl --grep` |

Every exclusion can be given several times. An entry is hidden when any of them
matches. `FIELD!=VALUE` needs no quoting in bash or zsh, because `!=` does not
trigger history expansion.

Example (real output, the entries were written with `systemd-cat`):

```console
$ journalctl -t demo-app -t demo-sync
Oct 27 05:45:36 default-vps demo-app[3378638]: request took 812 ms
Oct 27 05:45:36 default-vps demo-app[3378641]: health check ok
Oct 27 05:45:36 default-vps demo-app[3378644]: health check ok
Oct 27 05:45:36 default-vps demo-app[3378647]: upstream timed out
Oct 27 05:45:36 default-vps demo-sync[3378650]: Starting nightly sync
Oct 27 05:45:36 default-vps demo-app[3378653]: disk full, retrying
Oct 27 05:45:36 default-vps demo-app[3378658]: health check ok
$ journalctl-not -t demo-app -t demo-sync -n 3 PRIORITY!=7 -T demo-sync
Oct 27 05:45:36 default-vps demo-app[3378638]: request took 812 ms
Oct 27 05:45:36 default-vps demo-app[3378647]: upstream timed out
Oct 27 05:45:36 default-vps demo-app[3378653]: disk full, retrying
```

The "health check ok" lines were logged at debug priority (7). `-n 3` gives the
last three entries that are left, where `journalctl ... -n 3 | grep -v` would
have printed one.

A useful alias: `alias jn=journalctl-not`.

## Limitations

- Output modes: `short` (default), `short-precise`, `short-iso`,
  `short-iso-precise`, `short-full`, `short-unix`, `short-monotonic`, `cat` and
  `json`. Others (`verbose`, `export`, `json-pretty`, `with-unit` ...) are
  rejected with an error.
- `-x/--catalog` is ignored with a warning. With colors on, journalctl adds
  clickable links for `DOCUMENTATION=` and config file names and highlights
  `--grep` matches; journalctl-not does not. Without a terminal the output is
  identical.
- Month and weekday names are always English (journalctl uses `LC_TIME`).
- Exclusions run in Python after journalctl has read the entries, so they cost
  time instead of saving it: about 3.5 times journalctl's runtime when printing
  a whole journal of 600k lines, unnoticeable for `-f` or `-n 100`.
- `FIELD!=VALUE` compares the whole value. Entries without the field are kept,
  so `PRIORITY!=7` also keeps entries that have no priority, while `-p info`
  drops them.
- The formatting follows systemd 255. Newer versions may change details such
  as colors.
- Two journalctl 255 quirks are not reproduced: with `-n N --until T` and fewer
  than N matches, journalctl also prints entries after T; with `--grep` and
  `-n`, it prints them newest first.

## Development

```sh
python3 -m pip install pytest -e .
pytest
SUDO="sudo -n" tests/golden.sh   # byte-for-byte comparison with journalctl
```

## License

MIT
