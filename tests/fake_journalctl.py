#!/usr/bin/env python3
"""Stand-in for journalctl in tests.

Prints the JSON lines of $JNOT_FIXTURE, honoring --reverse and --after-cursor,
and appends its argv as one JSON line to $JNOT_ARGLOG.
"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["JNOT_ARGLOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if "--badopt" in args:
    print("journalctl: unrecognized option '--badopt'", file=sys.stderr)
    sys.exit(1)

with open(os.environ["JNOT_FIXTURE"]) as f:
    lines = [line for line in f if line.strip()]
after = next((a.split("=", 1)[1] for a in args if a.startswith("--after-cursor=")), None)
if after is not None:
    idx = [json.loads(line)["__CURSOR"] for line in lines].index(after)
    lines = lines[idx + 1:]
if "--reverse" in args:
    lines.reverse()
sys.stdout.write("".join(lines))
