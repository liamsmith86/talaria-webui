"""How setup looks in a terminal: one palette, and plain text everywhere else."""

import os
import sys

ACCENT = "1;36"
COMMAND = "36"
HEADING = "1"
SUCCESS = "1;32"
WARNING = "1;33"
ERROR = "1;31"
MUTED = "2"


def styled(text, code, stream=None):
    stream = sys.stdout if stream is None else stream
    if stream.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ:
        return f"\033[{code}m{text}\033[0m"
    return text


def print_section(title, rows, code=ACCENT):
    """Print a title over aligned rows of (label, value) or (label, value, style)."""
    print(f"\n{styled(title, code)}")
    width = max(len(label) for label, *_ in rows)
    for label, value, *style in rows:
        print(f"  {styled(label.ljust(width), MUTED)}  {styled(value, *style) if style else value}")
