"""Write test entries with awkward messages to the journal (native protocol)."""
import socket
import sys

tag = sys.argv[1]
sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)


def send(message, **fields):
    fields = {"SYSLOG_IDENTIFIER": tag, "PRIORITY": "6", "MESSAGE": message, **fields}
    data = b""
    for key, value in fields.items():
        if isinstance(value, str):
            value = value.encode()
        if b"\n" in value:
            data += key.encode() + b"\n" + len(value).to_bytes(8, "little") + value + b"\n"
        else:
            data += key.encode() + b"=" + value + b"\n"
    sock.sendto(data, "/run/systemd/journal/socket")


for prio in range(8):
    send(f"priority {prio}", PRIORITY=str(prio))
send("multi\nline\nmessage", PRIORITY="4")
send("trailing newline\n")
send("two trailing newlines\n\n")
send("")
send("tab\there")
send("control \x01 char")
send(b"invalid \xff utf-8")
send("\x1b]8;;https://example.com\x07hyperlink\x1b]8;;\x07 and \x1b[1;31mcolor\x1b[0m")
send("\x1b[2Acursor movement")
send("carriage return\r\n")
send("x" * 5000)
send("noise to exclude", PRIORITY="7")
send("unicode ü… ok")
send("no identifier", SYSLOG_IDENTIFIER="")
