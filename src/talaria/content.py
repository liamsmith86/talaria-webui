"""Bounded chat input and shared transcript content formatting."""

import base64
import binascii
import re

from .hermes import APIError

MAX_IMAGE = 2 * 1024 * 1024
MAX_IMAGES = 4
MAX_IMAGES_TOTAL = 6 * 1024 * 1024
MAX_CHAT_BODY = 9_500_000  # Below Hermes's 10 MB request boundary, including JSON.
IMAGE_URL = re.compile(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=]+)\Z")
REASONING = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def image_inputs(value):
    if not isinstance(value, list) or len(value) > MAX_IMAGES:
        raise APIError("Attach up to four images.", 400, "invalid_images")
    result, total = [], 0
    for item in value:
        url = item.get("url") if isinstance(item, dict) else None
        match = IMAGE_URL.fullmatch(url) if isinstance(url, str) else None
        if not match or len(url) > (MAX_IMAGE * 4 // 3) + 128:
            raise APIError(
                "Use PNG, JPEG, WebP, or GIF images up to 2 MB each.", 400, "invalid_images"
            )
        try:
            raw = base64.b64decode(match[2], validate=True)
        except binascii.Error as exc:
            raise APIError(
                "This image could not be read. Attach it again.", 400, "invalid_images"
            ) from exc
        kind = match[1]
        valid = {
            "png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
            "jpeg": raw.startswith(b"\xff\xd8\xff"),
            "gif": raw.startswith((b"GIF87a", b"GIF89a")),
            "webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
        }[kind]
        total += len(raw)
        if not valid or len(raw) > MAX_IMAGE or total > MAX_IMAGES_TOTAL:
            raise APIError(
                "Images must be valid and total no more than 6 MB.", 400, "invalid_images"
            )
        result.append({"type": "image_url", "image_url": {"url": url}})
    return result


def content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif part.get("type") in {"image_url", "input_image", "image"}:
            parts.append("[Image attachment — included in the JSON transcript]")
    return "\n\n".join(parts)
