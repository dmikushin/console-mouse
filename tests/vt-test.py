#!/usr/bin/python3
"""End-to-end test of console-mouse on a real VT. Run as root.

Briefly switches to a spare VT, runs a raw-mode reader there, drives a virtual
mouse, touchpad and keyboard through uinput, and checks what arrives:
  1. no mouse reporting: drag-select + Ctrl+Shift+C/V, double-click word +
     Alt+C/V paste exactly the selected text, and nothing else (the chords
     are voided in the keymap); with DECSET 2004 the paste is bracketed;
  2. DECSET 1000 (what tmux sends): wheel, click, drag, TrackPoint middle
     scroll, touchpad two-finger scroll and one/two-finger taps arrive as
     xterm X10 reports; the
     middle button is not reported; Shift+drag selects instead of reporting;
  3. a middle click sends {"action": "toggle"} to the console owner's
     nerd-dictation socket (a fake one here); the clipboard is not pasted
     into a console of another user;
  4. a real tmux scrolls its history with the wheel;
  5. the console keymap is restored when the daemon stops.
Then switches back to the original VT.
"""
import fcntl
import glob
import json
import os
import pwd
import socket
import threading
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "..", "console-mouse")
VT = 12

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
REL_X, REL_Y, REL_WHEEL = 0, 1, 8
ABS_X, ABS_Y = 0, 1
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
BTN_TOOL_FINGER, BTN_TOUCH, BTN_TOOL_DOUBLETAP = 0x145, 0x14A, 0x14D
KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, KEY_C, KEY_V = 29, 42, 56, 46, 47
# Owner of the test VT for the middle click: the daemon's dictation client
# runs as this user, so it must exist.
FAKE_UID = pwd.getpwnam("nobody").pw_uid
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_SET_ABSBIT, UI_SET_PROPBIT = 0x40045567, 0x4004556E
UI_DEV_SETUP, UI_ABS_SETUP = 0x405C5503, 0x401C5504
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
EVENT = struct.Struct("llHHi")

READER = r"""
import os, sys, termios, tty, time, select
mode, out, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
fd = sys.stdin.fileno()
tty.setraw(fd)
# Modes a previous program (tmux) may have left on this VT: reset them.
os.write(1, b"\033[?1000l\033[?2004l\033[H\033[2JHELLO WORLD\r\n")
if mode == "report":
    os.write(1, b"\033[?1000h")
if mode == "bracketed":
    os.write(1, b"\033[?2004h")
got = b""
end = time.time() + seconds
while time.time() < end:
    if select.select([fd], [], [], 0.05)[0]:
        got += os.read(fd, 1024)
os.write(1, b"\033[?1000l\033[?2004l")
open(out, "wb").write(got)
"""


def uinput(name, setup):
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    setup(fd)
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("4H80sI", 3, 1, 1, 1,
                                              name.encode(), 0))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def mouse_setup(fd):
    for ev in (EV_KEY, EV_REL):
        fcntl.ioctl(fd, UI_SET_EVBIT, ev)
    for b in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
        fcntl.ioctl(fd, UI_SET_KEYBIT, b)
    for r in (REL_X, REL_Y, REL_WHEEL):
        fcntl.ioctl(fd, UI_SET_RELBIT, r)


def keyboard_setup(fd):
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for k in (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, KEY_C, KEY_V):
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)


def chord(kb, *keys):
    for k in keys:
        emit(kb, (EV_KEY, k, 1))
    for k in reversed(keys):
        emit(kb, (EV_KEY, k, 0))


SCREEN_W, SCREEN_H = 1920, 1080   # touchscreen axis ranges in the test
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x2F, 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39


def touchscreen_setup(fd):
    for ev in (EV_KEY, EV_ABS):
        fcntl.ioctl(fd, UI_SET_EVBIT, ev)
    fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
    fcntl.ioctl(fd, UI_SET_PROPBIT, 1)      # INPUT_PROP_DIRECT
    for axis, maximum in ((ABS_X, SCREEN_W - 1), (ABS_Y, SCREEN_H - 1),
                          (ABS_MT_SLOT, 9), (ABS_MT_TRACKING_ID, 65535),
                          (ABS_MT_POSITION_X, SCREEN_W - 1),
                          (ABS_MT_POSITION_Y, SCREEN_H - 1)):
        fcntl.ioctl(fd, UI_SET_ABSBIT, axis)
        fcntl.ioctl(fd, UI_ABS_SETUP,
                    struct.pack("HH6i", axis, 0, 0, 0, maximum, 0, 0, 0))


class Touch:
    """Multitouch (protocol B) gestures on the virtual touchscreen, aimed
    at character cells of the VT."""

    def __init__(self, fd, cols, rows):
        self.fd, self.cols, self.rows = fd, cols, rows
        self.tid = 100

    def xy(self, col, row):
        return (int((col - 0.5) * SCREEN_W / self.cols),
                int((row - 0.5) * SCREEN_H / self.rows))

    def down(self, slot, col, row):
        x, y = self.xy(col, row)
        self.tid += 1
        return [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, self.tid),
                (EV_ABS, ABS_MT_POSITION_X, x), (EV_ABS, ABS_MT_POSITION_Y, y)]

    def move(self, slot, col, row):
        x, y = self.xy(col, row)
        return [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_POSITION_X, x),
                (EV_ABS, ABS_MT_POSITION_Y, y)]

    def up(self, slot):
        return [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1)]

    def tap(self, col, row):
        emit(self.fd, *self.down(0, col, row), (EV_KEY, BTN_TOUCH, 1))
        emit(self.fd, *self.up(0), (EV_KEY, BTN_TOUCH, 0))

    def two_tap(self, col, row):
        emit(self.fd, *self.down(0, col, row), *self.down(1, col + 3, row),
             (EV_KEY, BTN_TOUCH, 1))
        emit(self.fd, *self.up(0), *self.up(1), (EV_KEY, BTN_TOUCH, 0))

    def drag(self, cells):
        emit(self.fd, *self.down(0, *cells[0]), (EV_KEY, BTN_TOUCH, 1))
        for c in cells[1:]:
            emit(self.fd, *self.move(0, *c))
        emit(self.fd, *self.up(0), (EV_KEY, BTN_TOUCH, 0))

    def two_drag(self, col, rows):
        emit(self.fd, *self.down(0, col, rows[0]), *self.down(1, col + 3, rows[0]),
             (EV_KEY, BTN_TOUCH, 1))
        for r in rows[1:]:
            emit(self.fd, *self.move(0, col, r), *self.move(1, col + 3, r))
        emit(self.fd, *self.up(0), *self.up(1), (EV_KEY, BTN_TOUCH, 0))


def touchpad_setup(fd):
    for ev in (EV_KEY, EV_ABS):
        fcntl.ioctl(fd, UI_SET_EVBIT, ev)
    for b in (BTN_LEFT, BTN_TOOL_FINGER, BTN_TOUCH, BTN_TOOL_DOUBLETAP):
        fcntl.ioctl(fd, UI_SET_KEYBIT, b)
    fcntl.ioctl(fd, UI_SET_PROPBIT, 0)      # INPUT_PROP_POINTER
    for axis, maximum in ((ABS_X, 2000), (ABS_Y, 1300)):
        fcntl.ioctl(fd, UI_SET_ABSBIT, axis)
        fcntl.ioctl(fd, UI_ABS_SETUP,
                    struct.pack("HH6i", axis, 0, 0, 0, maximum, 0, 0, 20))


def emit(fd, *events):
    for type_, code, value in events:
        os.write(fd, EVENT.pack(0, 0, type_, code, value))
    os.write(fd, EVENT.pack(0, 0, EV_SYN, 0, 0))
    time.sleep(0.05)


def home(m):
    """Pointer to cell (1, 1): huge negative motion is clamped."""
    emit(m, (EV_REL, REL_X, -100000), (EV_REL, REL_Y, -100000))


def goto(m, col, row):
    home(m)
    emit(m, (EV_REL, REL_X, (col - 1) * 8), (EV_REL, REL_Y, (row - 1) * 16))


def click(m, btn):
    emit(m, (EV_KEY, btn, 1))
    emit(m, (EV_KEY, btn, 0))


def run_reader(mode, seconds, actions):
    out = tempfile.mktemp()
    script = tempfile.mktemp(suffix=".py")
    open(script, "w").write(READER)
    subprocess.run(["openvt", "-c", str(VT), "-f", "--", "python3", script,
                    mode, out, str(seconds)], check=True)
    subprocess.run(["chvt", str(VT)], check=True)
    time.sleep(0.5)
    actions()
    while not os.path.exists(out):
        time.sleep(0.1)
    time.sleep(0.1)
    return open(out, "rb").read()


def touchscreen(ts):
    """Tap = click at the touched cell; two-finger tap = right click; one
    finger dragged = left-button drag; two fingers dragged down 5 rows = 2
    wheel steps up (the content follows the fingers)."""
    vt = f"/dev/tty{VT}"
    rows, cols, _, _ = struct.unpack(
        "4H", fcntl.ioctl(os.open(vt, os.O_RDONLY | os.O_NOCTTY), 0x5413, bytes(8)))
    touch = Touch(ts, cols, rows)

    def gestures():
        touch.tap(10, 5)
        time.sleep(0.5)             # not a double click
        touch.two_tap(20, 6)
        time.sleep(0.5)
        touch.drag([(5, 3), (6, 3), (7, 3), (8, 3)])
        touch.two_drag(30, [10, 11, 12, 13, 14, 15])

    got = run_reader("report", 4.0, gestures)
    failures = 0
    expected = (r(0, 10, 5) + r(3, 10, 5)              # tap
                + r(2, 10, 5) + r(3, 10, 5)            # two-finger tap
                + r(0, 5, 3) + r(32, 7, 3) + r(32, 8, 3) + r(3, 8, 3)  # drag
                + r(64, 8, 3) * 2)                     # two-finger scroll
    failures += check("touchscreen: tap, two-finger tap, drag, two-finger scroll",
                      got, expected)

    # Rotated console (fbcon rotate_all 1 = 90 degrees clockwise): the
    # console's top-left is at the panel's top-right, so console cell
    # (col, row) is touched at panel x = right - row, y = col.
    rotate = "/sys/class/graphics/fbcon/rotate_all"
    if not os.path.exists(rotate):
        print("SKIP: rotated touchscreen (no fbcon rotation)")
        return failures
    with open(rotate, "w") as f:
        f.write("1")
    try:
        time.sleep(0.5)
        rows, cols, _, _ = struct.unpack("4H", fcntl.ioctl(
            os.open(vt, os.O_RDONLY | os.O_NOCTTY), 0x5413, bytes(8)))
        col, row = 6, 4

        def rotated_tap():
            x = int(SCREEN_W - (row - 0.5) * SCREEN_W / rows)
            y = int((col - 0.5) * SCREEN_H / cols)
            emit(ts, (EV_ABS, ABS_MT_SLOT, 0), (EV_ABS, ABS_MT_TRACKING_ID, 999),
                 (EV_ABS, ABS_MT_POSITION_X, x), (EV_ABS, ABS_MT_POSITION_Y, y),
                 (EV_KEY, BTN_TOUCH, 1))
            emit(ts, (EV_ABS, ABS_MT_SLOT, 0), (EV_ABS, ABS_MT_TRACKING_ID, -1),
                 (EV_KEY, BTN_TOUCH, 0))

        got = run_reader("report", 2.0, rotated_tap)
        failures += check("touchscreen on a console rotated 90 degrees",
                          got, r(0, col, row) + r(3, col, row))
    finally:
        with open(rotate, "w") as f:
            f.write("0")
    return failures


def r(code, col, row):
    return b"\033[M" + bytes([32 + code, 32 + col, 32 + row])


def daemon_module():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("daemon", DAEMON)
    spec = importlib.util.spec_from_loader("daemon", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def keymap_entries(tables=(5, 7, 8, 10)):
    # Through a Unicode-mode VT: through /dev/tty0 with X in front, Unicode
    # entries would read as holes (see unicode_vt in the daemon).
    fd = daemon_module().unicode_vt()
    out = []
    for table in tables:
        for key in (KEY_C, KEY_V):
            buf = bytearray(struct.pack("BBH", table, key, 0))
            fcntl.ioctl(fd, 0x4B46, buf)        # KDGKBENT
            out.append(struct.unpack("BBH", buf)[2])
    os.close(fd)
    return out


def fake_dictation():
    """A stand-in for nerd-dictation's daemon socket; returns received
    commands."""
    rundir = f"/run/user/{FAKE_UID}"
    os.makedirs(rundir, exist_ok=True)
    os.chown(rundir, FAKE_UID, -1)
    path = f"{rundir}/nerd-dictation-ex.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chown(path, FAKE_UID, -1)
    server.listen()
    commands = []

    def serve():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            commands.append(json.loads(client.recv(4096)))
            client.send(b'{"status": "ok"}')
            client.close()

    threading.Thread(target=serve, daemon=True).start()

    def close():
        server.close()
        os.unlink(path)
        os.rmdir(rundir)
    return commands, close


KEYMAP_STATE = "/run/console-mouse/keymap-original"


def kb_entry(fd, table, key, value=None):
    if value is None:
        buf = bytearray(struct.pack("BBH", table, key, 0))
        fcntl.ioctl(fd, 0x4B46, buf)            # KDGKBENT
        return struct.unpack("BBH", buf)[2]
    fcntl.ioctl(fd, 0x4B47, struct.pack("BBH", table, key, value))   # KDSKBENT


def keymap_snapshot(fd):
    """Every entry of every allocated table, read through a Unicode VT."""
    return {(t, k): kb_entry(fd, t, k) for t in range(256)
            if kb_entry(fd, t, 0) != 0x027F for k in range(256)}


def unicode_keymap_test():
    """The daemon must handle Unicode keymap entries even when the VT in
    front is not in Unicode mode (X): read through such a VT they look like
    holes. A Unicode value (U+0441) is put into a chord it voids (Ctrl+Shift
    +C, table 5): it must be voided while the daemon runs and come back after
    a clean stop and after SIGKILL + restart + stop; the whole keymap must
    be unchanged."""
    fd = daemon_module().unicode_vt()
    front = open("/sys/class/tty/tty0/active").read().strip()
    tfd = os.open("/dev/tty0", os.O_RDONLY | os.O_NOCTTY)
    mode = bytearray(4)
    fcntl.ioctl(tfd, 0x4B44, mode)              # KDGKBMODE
    os.close(tfd)
    print(f"  VT in front: {front}, keyboard mode {struct.unpack('i', mode)[0]}"
          f" (3 = Unicode; X is not)")
    before = keymap_snapshot(fd)
    slot, value = (5, KEY_C), 0xf441
    original = kb_entry(fd, *slot)
    kb_entry(fd, *slot, value)
    seen = []
    try:
        d = subprocess.Popen([sys.executable, DAEMON])
        time.sleep(1.2)
        seen.append(kb_entry(fd, *slot))        # voided while running
        d.terminate(); d.wait()
        seen.append(kb_entry(fd, *slot))        # back after a clean stop
        d = subprocess.Popen([sys.executable, DAEMON])
        time.sleep(1.2)
        d.kill(); d.wait()
        d = subprocess.Popen([sys.executable, DAEMON])
        time.sleep(1.2)
        d.terminate(); d.wait()
        seen.append(kb_entry(fd, *slot))        # back after SIGKILL + restart
    finally:
        kb_entry(fd, *slot, original)
        after = keymap_snapshot(fd)
        os.close(fd)
    failures = check("Unicode entry voided and kept with X in front (run, stop, kill+restart)",
                     " ".join(f"{v:#06x}" for v in seen).encode(),
                     b"0x0200 0xf441 0xf441")
    failures += check("whole keymap unchanged", str(after == before).encode(),
                      b"True")
    return failures


def keymap_crash():
    """A daemon killed with SIGKILL leaves its holes in the kernel keymap;
    the next instance must restore the real originals from the state file,
    not the holes it finds. Keycodes C and V in all 256 tables are compared
    with their values before the test."""
    every = range(256)
    before = keymap_entries(every)
    failures = 0
    first = subprocess.Popen([sys.executable, DAEMON])
    for _ in range(50):                     # until it has voided the chords
        time.sleep(0.1)
        if keymap_entries(every) != before:
            break
    first.kill()
    first.wait()
    failures += check("SIGKILL leaves the originals on disk",
                      str(os.path.exists(KEYMAP_STATE)).encode(), b"True")
    second = subprocess.Popen([sys.executable, DAEMON],
                              stdout=subprocess.PIPE, text=True)
    time.sleep(1.0)
    second.terminate()
    log = second.communicate()[0]
    failures += check("next instance takes the originals from the file",
                      str("left by an instance that did not restore" in log).encode(),
                      b"True")
    failures += check("keymap C/V in all tables restored after SIGKILL + restart",
                      str(keymap_entries(every)).encode(), str(before).encode())
    failures += check("state file removed after a clean restore",
                      str(os.path.exists(KEYMAP_STATE)).encode(), b"False")
    return failures


def console_locked():
    """A console lock (Win+Escape) blocks every VT switch: the test would
    hang on chvt."""
    if os.path.exists("/run/tty-i3-keys/lock.json"):
        return True
    for comm in glob.glob("/proc/[0-9]*/comm"):
        try:
            if open(comm).read().strip() in ("ttysecurelock", "vlock"):
                return True
        except OSError:
            pass
    return False


def main():
    only = sys.argv[1:]                     # e.g. keymap-crash
    # One VT test at a time (tty-i3-keys' and ttysecurelock's take it too).
    lockfile = open("/tmp/vt12-test.lock", "w")
    fcntl.flock(lockfile, fcntl.LOCK_EX)
    if console_locked():
        sys.exit("the console is locked (Win+Escape): unlock it first")
    if only in (["keymap-crash"], ["unicode"]):
        service = subprocess.run(["systemctl", "is-active", "--quiet",
                                  "console-mouse"]).returncode == 0
        if service:
            subprocess.run(["systemctl", "stop", "console-mouse"], check=True)
        try:
            failures = keymap_crash() if only == ["keymap-crash"] else unicode_keymap_test()
        finally:
            if service:
                subprocess.run(["systemctl", "start", "console-mouse"])
        sys.exit(1 if failures else 0)
    original = open("/sys/class/tty/tty0/active").read().strip()[3:]
    # An installed daemon would answer the same events a second time.
    service = subprocess.run(["systemctl", "is-active", "--quiet",
                              "console-mouse"]).returncode == 0
    if service:
        subprocess.run(["systemctl", "stop", "console-mouse"], check=True)
    # Before any VT switch: with X in front, as on a laptop running i3.
    unicode_failures = unicode_keymap_test()
    keymap_before = keymap_entries()
    m = uinput("console-mouse test mouse", mouse_setup)
    tp = uinput("console-mouse test touchpad", touchpad_setup)
    ts = uinput("console-mouse test touchscreen", touchscreen_setup)
    kb = uinput("console-mouse test keyboard", keyboard_setup)
    commands, close_dictation = fake_dictation()
    time.sleep(0.3)
    daemon = subprocess.Popen([sys.executable, DAEMON])
    time.sleep(0.5)
    failures = unicode_failures
    try:
        def copy_paste():
            goto(m, 1, 1)                        # drag-select "HELLO"
            emit(m, (EV_KEY, BTN_LEFT, 1))
            emit(m, (EV_REL, REL_X, 4 * 8))
            emit(m, (EV_KEY, BTN_LEFT, 0))
            chord(kb, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_C)
            chord(kb, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V)
            goto(m, 9, 1)                        # double-click "WORLD"
            click(m, BTN_LEFT)
            click(m, BTN_LEFT)
            chord(kb, KEY_LEFTALT, KEY_C)
            chord(kb, KEY_LEFTALT, KEY_V)
            goto(m, 1, 1)                        # click clears the selection,
            click(m, BTN_LEFT)                   # copy keeps the clipboard
            time.sleep(0.5)
            chord(kb, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_C)
            chord(kb, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V)

        got = run_reader("plain", 3.5, copy_paste)
        failures += check("select + Ctrl+Shift+C/V, Alt+C/V",
                          got, b"HELLOWORLDWORLD")

        got = run_reader("bracketed", 1.5,
                         lambda: chord(kb, KEY_LEFTALT, KEY_V))
        failures += check("bracketed paste",
                          got, b"\033[200~WORLD\033[201~")

        def reports():
            goto(m, 3, 2)
            emit(m, (EV_REL, REL_WHEEL, 1))
            emit(m, (EV_REL, REL_WHEEL, -1))
            click(m, BTN_LEFT)
            emit(m, (EV_KEY, BTN_LEFT, 1))
            emit(m, (EV_REL, REL_X, 8))          # drag to column 4
            emit(m, (EV_KEY, BTN_LEFT, 0))
            emit(m, (EV_KEY, BTN_MIDDLE, 1))     # TrackPoint scroll: 2 down
            emit(m, (EV_REL, REL_Y, 24))
            emit(m, (EV_KEY, BTN_MIDDLE, 0))
            # Touchpad: two fingers 6 mm up = 2 steps up (pointer stays).
            emit(tp, (EV_KEY, BTN_TOUCH, 1), (EV_KEY, BTN_TOOL_DOUBLETAP, 1),
                 (EV_ABS, ABS_X, 1000), (EV_ABS, ABS_Y, 600))
            emit(tp, (EV_ABS, ABS_Y, 540))
            emit(tp, (EV_ABS, ABS_Y, 480))
            emit(tp, (EV_KEY, BTN_TOUCH, 0), (EV_KEY, BTN_TOOL_DOUBLETAP, 0))
            # Taps: one finger = left click, two fingers = right click.
            for tool in (BTN_TOOL_FINGER, BTN_TOOL_DOUBLETAP):
                emit(tp, (EV_KEY, BTN_TOUCH, 1), (EV_KEY, tool, 1),
                     (EV_ABS, ABS_X, 1000), (EV_ABS, ABS_Y, 600))
                emit(tp, (EV_KEY, BTN_TOUCH, 0), (EV_KEY, tool, 0))
            # Shift+drag selects "HELLO" instead of reporting; then paste it.
            goto(m, 1, 1)
            emit(kb, (EV_KEY, KEY_LEFTSHIFT, 1))
            emit(m, (EV_KEY, BTN_LEFT, 1))
            emit(m, (EV_REL, REL_X, 4 * 8))
            emit(m, (EV_KEY, BTN_LEFT, 0))
            emit(kb, (EV_KEY, KEY_LEFTSHIFT, 0))
            chord(kb, KEY_LEFTALT, KEY_C)
            chord(kb, KEY_LEFTALT, KEY_V)
            # Still middle click: dictation toggle, not a report.
            os.chown(f"/dev/tty{VT}", FAKE_UID, -1)
            click(m, BTN_MIDDLE)
            time.sleep(0.5)
            # The VT now belongs to another user: the clipboard copied while
            # it was root's must not be pasted into it.
            chord(kb, KEY_LEFTALT, KEY_V)

        got = run_reader("report", 4.0, reports)
        expected = (r(64, 3, 2) + r(65, 3, 2)          # wheel up, down
                    + r(0, 3, 2) + r(3, 3, 2)          # left click
                    + r(0, 3, 2) + r(32, 4, 2) + r(3, 4, 2)  # drag
                    + r(65, 4, 2) * 2                  # middle-button scroll
                    + r(64, 4, 2) * 2                  # two-finger scroll
                    + r(0, 4, 2) + r(3, 4, 2)          # one-finger tap
                    + r(2, 4, 2) + r(3, 4, 2)          # two-finger tap
                    + b"HELLO")                        # Shift+drag, Alt+C/V
        failures += check("xterm reports (tmux mouse), Shift bypass",
                          got, expected)
        failures += check("middle click toggles nerd-dictation",
                          json.dumps(commands).encode(),
                          b'[{"action": "toggle"}]')
        failures += touchscreen(ts)
        failures += tmux_scroll(m)
    finally:
        daemon.terminate()
        daemon.wait()
        failures += check("keymap restored on stop",
                          str(keymap_entries()).encode(),
                          str(keymap_before).encode())
        failures += keymap_crash()
        subprocess.run(["chvt", original])
        for fd in (m, tp, kb, ts):
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        close_dictation()
        if service:
            subprocess.run(["systemctl", "start", "console-mouse"])
    sys.exit(1 if failures else 0)


def tmux_scroll(m):
    """A real tmux with "mouse on" on the VT. tmux's default bindings: the
    first WheelUpPane only enters copy mode, each further wheel step scrolls
    5 lines. So 3 steps up = 10 lines back, then 1 step down = 5."""
    conf = tempfile.mktemp()
    open(conf, "w").write("set -g mouse on\n")
    tmux = ["tmux", "-L", "console-mouse-test", "-f", conf]
    log = tempfile.mktemp()
    # A VT login has TERM=linux; openvt passes on ours, which may be unset.
    subprocess.run(["openvt", "-c", str(VT), "-f", "--", "sh", "-c",
                    'TERM=linux exec "$@" 2>"$0"', log, *tmux, "new-session",
                    "seq 1 500; sleep 30"], check=True)
    subprocess.run(["chvt", str(VT)], check=True)
    time.sleep(1.5)
    try:
        goto(m, 10, 10)
        position = lambda: subprocess.run(
            [*tmux, "display", "-p", "#{pane_in_mode} #{scroll_position}"],
            capture_output=True, text=True).stdout.strip()
        for _ in range(3):
            emit(m, (EV_REL, REL_WHEEL, 1))
        time.sleep(0.5)
        got = position()
        emit(m, (EV_REL, REL_WHEEL, -1))
        time.sleep(0.5)
        got += " / " + position()
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True)
    if os.path.exists(log) and os.path.getsize(log):
        print("  tmux stderr:", open(log).read().strip())
    return check("real tmux: wheel scrolls the pane history",
                 got.encode(), b"1 10 / 1 5")


def check(name, got, expected):
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not ok:
        print(f"  expected {expected!r}\n  got      {got!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
