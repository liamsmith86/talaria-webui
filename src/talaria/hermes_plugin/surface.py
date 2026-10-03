"""Describe Talaria's renderer without changing the profile's persona or history."""

from .request_log import in_talaria_run

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


def shape_request(*, request=None, platform=None, **kwargs):
    """Use Hermes's request middleware; leave native agents and stored prompts intact."""
    if platform != "api_server" or not in_talaria_run() or not isinstance(request, dict):
        return None
    from agent.prompt_builder import PLATFORM_HINTS

    original = PLATFORM_HINTS.get("api_server")
    if not original:
        return None
    shaped = dict(request)
    for key in ("system", "instructions"):
        if key in shaped:
            shaped[key] = replace_hint(shaped[key], original)
    messages = shaped.get("messages") or shaped.get("input")
    if (
        isinstance(messages, list)
        and messages
        and isinstance(messages[0], dict)
        and messages[0].get("role") in {"system", "developer"}
    ):
        first = messages[0]
        field = "messages" if "messages" in shaped else "input"
        shaped[field] = [
            {**first, "content": replace_hint(first.get("content"), original)},
            *messages[1:],
        ]
    if shaped != request:
        return {"request": shaped, "source": "talaria", "reason": "web interface formatting"}
    return None
