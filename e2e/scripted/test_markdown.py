"""Markdown as people read it, while it streams and after a reload."""

from playwright.sync_api import expect
from scripted_model import plan

REPLY = """| Name | Age |
| --- | --- |
| Ann | 30 |

```python
print("hello")
```
"""


def test_markdown_and_code_render(chat):
    chat.send(plan(REPLY))
    text = chat.page.locator(".message.assistant").last.locator(".message-text")
    expect(text.locator("table")).to_contain_text("Ann")
    expect(text.locator("pre code")).to_contain_text("print")
    expect(text.get_by_role("button", name="Copy code")).to_be_visible()
