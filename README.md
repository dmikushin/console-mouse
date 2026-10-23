# console-mouse

Mouse on the Linux text console, including wheel scrolling in tmux, with
terminal-emulator style copy and paste.

gpm never forwards the wheel to programs, and the kernel's own mouse report
(`TIOCL_SELMOUSEREPORT`) can encode buttons 0-15 only, while the xterm wheel
codes are 64 and 65. `console-mouse` replaces gpm with one Python script
(standard library only) that reads mice, TrackPoints, touchpads and keyboards
through evdev and, while the foreground VT is in text mode:

| | program did not ask for mouse reports | program asked (DECSET 9 / 1000, e.g. tmux `mouse on`) |
|---|---|---|
| left | drag selects; double click: words, triple: lines | xterm report |
| right | extends the selection | xterm report |
| wheel | - | xterm report |
| Shift + mouse | selects | selects (bypasses the program, as in xterm) |
| middle click | toggles nerd-dictation | toggles nerd-dictation |

| keys | action |
|---|---|
| Ctrl+Shift+C, Alt+C | copy the selection |
| Ctrl+Shift+V, Alt+V | paste (bracketed if the program enabled DECSET 2004) |

The four chords are voided in the console keymap while the daemon runs (and
restored when it stops), so the program does not also receive ^C, ^V, ESC c
or ESC v. Reports and pastes are injected into the tty input with `TIOCSTI`;
drag motion is reported too, so tmux copy-mode selection by dragging works. The original entries are kept in
`/run/console-mouse/keymap-original` from before the first change until a
clean restore, so an instance that was killed is undone by the next one.

A middle click sends `{"action": "toggle"}` to the nerd-dictation daemon of
the user who owns the console (`/run/user/<uid>/nerd-dictation-ex.sock`),
the same toggle the middle button does under X. The middle button is never
passed to programs.

Touchscreen: a tap clicks the touched cell, a two-finger tap is a right
click, one finger dragged drags with the left button (selection, tmux copy
mode), and two fingers dragged scroll the content along with the fingers.

Wheel sources: a mouse wheel, a TrackPoint moved with the middle button held,
two fingers on a touchpad. A two-finger touchpad click is a right click.

Under X (the VT is in `KD_GRAPHICS`) the daemon does nothing.

## Install

```sh
sudo make install
```

Installs `/usr/local/bin/console-mouse` and enables `console-mouse.service`.
Do not run gpm at the same time.

## Test

```sh
sudo make test
```

Switches to VT 12 for about 15 seconds, drives a virtual mouse, touchpad,
touchscreen and keyboard through uinput, and checks what a program on that VT receives:
copy and paste by both chords (bracketed and not), the exact xterm report
bytes, the Shift bypass, the dictation toggle (against a fake socket), that
a real tmux scrolls its history with the wheel, and that the keymap is
restored - also after a SIGKILL (`sudo python3 tests/vt-test.py keymap-crash`
runs only that case). Switches back afterwards.

## Tuning

Pointer speed and scroll step are constants at the top of `console-mouse`
(`REL_COLS_PER_COUNT`, `TP_COLS_PER_MM`, `REL_SCROLL_COUNTS`,
`TP_SCROLL_MM`).
