"""Describe Talaria's renderer without changing the profile's persona or history."""

from copy import deepcopy
from functools import wraps

HINT = (
    "You're responding in Talaria, a web interface to Hermes Agent. "
    "The chat renders Markdown, including headings, lists, tables, and fenced code blocks. "
    "Use formatting when it helps the user. "
    "Tools and files run on the Hermes host; local file paths are not browser download links. "
    "MEDIA: tags are not delivered as attachments by this interface."
)


def replace_hint(content, original):
    if isinstance(content, str):
        return content.replace(original, HINT, 1)
    if isinstance(content, list):
        return [
            {**block, "text": replace_hint(block["text"], original)}
            if isinstance(block, dict) and isinstance(block.get("text"), str)
            else block
            for block in content
        ]
    return content


def apply_surface(agent):
    from agent.prompt_builder import PLATFORM_HINTS

    original = PLATFORM_HINTS.get("api_server")
    if not original or not callable(getattr(agent, "_build_api_kwargs", None)):
        return
    overrides = deepcopy(getattr(agent, "_platform_hint_overrides", {}) or {})
    spec = overrides.get("api_server")
    if isinstance(spec, dict) and isinstance(spec.get("replace"), str) and spec["replace"].strip():
        return  # A deliberate profile instruction outranks our renderer description.
    spec = {"append": spec} if isinstance(spec, str) else spec
    overrides["api_server"] = {**(spec if isinstance(spec, dict) else {}), "replace": HINT}
    agent._platform_hint_overrides = overrides
    build = agent._build_api_kwargs

    @wraps(build)
    def build_for_surface(*args, **kwargs):
        # Older sessions restore their cached system prompt. Adjust only the known
        # built-in hint on the wire; Hermes's stored prompt and history stay intact.
        request = {**build(*args, **kwargs)}
        for key in ("system", "instructions"):
            if key in request:
                request[key] = replace_hint(request[key], original)
        messages = request.get("messages") or request.get("input")
        if (
            isinstance(messages, list)
            and messages
            and isinstance(messages[0], dict)
            and messages[0].get("role") in {"system", "developer"}
        ):
            first = messages[0]
            field = "messages" if "messages" in request else "input"
            request[field] = [
                {**first, "content": replace_hint(first.get("content"), original)},
                *messages[1:],
            ]
        return request

    agent._build_api_kwargs = build_for_surface
