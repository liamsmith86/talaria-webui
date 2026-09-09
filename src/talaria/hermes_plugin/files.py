"""Bounded regular-file reads inside Hermes, never in the Talaria web process."""

import os
import stat


def read_text(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Unsupported Hermes file")
        data = file.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Oversized Hermes file")
        return data.decode("utf-8-sig")
