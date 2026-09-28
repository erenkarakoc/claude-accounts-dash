"""Small terminal UI helpers: colors, a progress line and status symbols (stdlib only).

Colors are off when the output is not a terminal, or when NO_COLOR is set.
"""
import os
import sys
import time

_ON = False


def setup():
    """Enable colors/Unicode where the terminal supports them. Call once at startup."""
    global _ON
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    global OK, WARN, FAIL, ARROW, DOT, SEP, DASH, SPIN, BLOCKS
    try:
        "✓✗➜●█░━⠋▁▄·—".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        OK, WARN, FAIL, ARROW, DOT, SEP, DASH = "+", "!", "x", ">", "*", "|", "-"
        SPIN, BLOCKS = "|/-\\", ("#", ".", "=", "_", "o", ".")
    tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    _ON = tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    if _ON and sys.platform == "win32":
        try:  # turn on ANSI escape handling in the Windows console
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            _ON = False
    return _ON


def interactive():
    return _ON


def _c(code):
    return lambda s: f"\x1b[{code}m{s}\x1b[0m" if _ON else str(s)


bold, dim, italic = _c("1"), _c("2"), _c("3")
green, yellow, red, cyan, magenta, gray = _c("32"), _c("33"), _c("31"), _c("36"), _c("35"), _c("90")
bgreen = _c("1;32")

OK, WARN, FAIL, ARROW, DOT, SEP, DASH = "✓", "!", "✗", "➜", "●", "·", "—"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BLOCKS = ("█", "░", "━", "▁", "▄", "·")     # full, empty, line, low, mid, none


def logo():
    """The favicon's three rising bars, in color."""
    return f"{gray(BLOCKS[3])}{gray(BLOCKS[4])}{green(BLOCKS[0])}"


def bar(pct, width=12):
    """A meter like ████░░░░ colored by level."""
    if pct is None:
        return gray(BLOCKS[5] * width)
    p = max(0, min(100, pct))
    full = round(p / 100 * width)
    color = red if p >= 100 else yellow if p >= 70 else green
    return color(BLOCKS[0] * full) + gray(BLOCKS[1] * (width - full))


def pct_text(pct):
    if pct is None:
        return gray("  " + DASH)
    color = red if pct >= 100 else yellow if pct >= 70 else green
    return color(f"{pct:>3}%")


class Progress:
    """A single self-updating line: spinner, label, bar and counter."""

    def __init__(self, label):
        self.label, self.i, self.last = label, 0, 0.0

    def update(self, done, total):
        if not _ON:
            return
        now = time.time()
        if now - self.last < 0.06 and done < total:
            return
        self.last, self.i = now, self.i + 1
        frac = done / total if total else 1
        width = 24
        full = int(frac * width)
        line = (f"  {cyan(SPIN[self.i % len(SPIN)])} {self.label}  "
                f"{cyan(BLOCKS[2] * full)}{gray(BLOCKS[2] * (width - full))}  {gray(f'{done:,}/{total:,}')}")
        sys.stdout.write("\r\x1b[2K" + line)
        sys.stdout.flush()

    def clear(self):
        if _ON:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()


def visible_len(s):
    """Length of a string without ANSI escapes (for column alignment)."""
    import re
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


def pad(s, width):
    return s + " " * max(0, width - visible_len(s))
