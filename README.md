# console-mouse

Mouse on the Linux text console, including wheel scrolling in tmux, with
terminal-emulator style copy and paste.

gpm never forwards the wheel to programs, and the kernel's own mouse report
(`TIOCL_SELMOUSEREPORT`) can encode buttons 0-15 only, while the xterm wheel
codes are 64 and 65. `console-mouse` replaces gpm with one Python script
(standard library only) that reads mice, TrackPoints, touchpads and keyboards
through evdev and, while the foreground VT is in text mode:

The selection belongs to the console, as in a graphical terminal, whatever
the program in front asked for:

| mouse | program did not ask for mouse reports | program asked (DECSET 9 / 1000, e.g. tmux `mouse on`) |
|---|---|---|
| left drag | selects | selects (not reported) |
| left double / triple click | selects a word / a line | selects a word / a line (the first click is reported) |
| left click without movement | clears the selection | clears it; press + release reported on release |
| right click | extends the selection | xterm report (tmux menus) |
| wheel | - | xterm report (tmux scrollback) |
| Shift + left button | selects | reported, drag included (tmux pane borders, mc) |
| middle click | toggles nerd-dictation | toggles nerd-dictation |

Shift is a deliberate inversion of the earlier behaviour (and of xterm's):
there the program got every drag and Shift selected; now selecting is the
default and Shift hands the drag to the program.

| keys | action |
|---|---|
| Ctrl+Shift+C, Alt+C, Ctrl+Alt+C, Ctrl+Insert | copy the selection |
| Ctrl+Shift+V, Alt+V, Ctrl+Alt+V, Shift+Insert | paste (bracketed if the program enabled DECSET 2004) |

Selecting does not copy: there is no PRIMARY to paste with a middle click,
the middle button belongs to dictation, so a copy chord is needed, as with
Ctrl+Shift+C in a graphical terminal.

These chords are voided in the console keymap while the daemon runs (and
restored when it stops), so the program does not also receive ^C, ^V, ESC c,
ESC v, ESC ^C, ESC ^V or Insert. The chords are keycodes, so they
work the same in the Cyrillic layout (the AltGr lock of `ruwin_cplk`).
Reports and pastes are injected into the tty input with `TIOCSTI`. The original entries are kept in
`/run/console-mouse/keymap-original` from before the first change until a
clean restore, so an instance that was killed is undone by the next one.

What is copied is the text of the highlighted cells at the moment of
selecting, with the kernel's rules: trailing blanks of a row dropped, a line
break after each row that ends in blanks. The selection is of screen cells:
across tmux panes it takes the other pane and the border, as a graphical
terminal does. Pasted text is typed into the VT: tmux passes it to the
active pane, a tmux inside it to its own active pane, and the brackets
reach the innermost program only if it enabled 2004 (tested).

The kernel removes the highlight on any write to the console (`con_write`
-> `hide_cursor` -> `clear_selection`), and tmux writes all the time. The
daemon waits for updates of the VT (`POLLPRI` on `/dev/vcsaN`) and puts the
highlight back while the selected cells still show the selected text; once
they change, the highlight is gone, and a copy still copies what was
selected.

A middle click sends `{"action": "toggle"}` to the nerd-dictation daemon of
the user who owns the console (`/run/user/<uid>/nerd-dictation-ex.sock`),
the same toggle the middle button does under X. The middle button is never
passed to programs.

Touchscreen: a tap clicks the touched cell, a two-finger tap is a right
click, one finger dragged drags with the left button (selects), and two
fingers dragged scroll the content along with the fingers.

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

Switches to VT 12 for about two minutes, drives a virtual mouse, touchpad,
touchscreen and keyboard through uinput, and checks what a program on that VT receives:
copy and paste by every chord (bracketed and not) in a bare shell, in tmux
and in tmux inside tmux, the exact xterm report bytes, Shift+drag reaching
the program, pane and window selection by a click and pane resizing by
Shift+drag in tmux, the highlight surviving writes elsewhere, the dictation toggle (against a fake socket), that
a real tmux scrolls its history with the wheel, and that the keymap is
restored - also after a SIGKILL (`sudo python3 tests/vt-test.py keymap-crash`
runs only that case). Switches back afterwards.

## Tuning

Pointer speed and scroll step are constants at the top of `console-mouse`
(`REL_COLS_PER_COUNT`, `TP_COLS_PER_MM`, `REL_SCROLL_COUNTS`,
`TP_SCROLL_MM`).
