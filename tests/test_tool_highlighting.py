"""Tool coloring stays lazy, bounded, and faithful to the original source."""

import json

from playwright.sync_api import expect

from .test_frontend_ui_qa import open_notes

COMMAND = '''python3 -c "
import json
data = {'messages': [{'id': 42, 'content': '<script>not executable</script>'}]}
# Read only the synthetic fixture
last = [message for message in data['messages'] if message['id'] == 42][0]
print(last['content'])
"'''


def test_tool_commands_highlight_only_when_open_and_keep_their_nodes(page, live_app):
    page.context.add_init_script("""addEventListener('DOMContentLoaded', () => {
      const tokenize = Prism.tokenize;
      window.toolTokenizations = 0;
      Prism.tokenize = function(...args) {
        window.toolTokenizations++;
        return tokenize.apply(this, args);
      };
    });""")
    sid = open_notes(
        page,
        live_app,
        [
            {
                "id": 1,
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "terminal-code",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": COMMAND}),
                        },
                    }
                ],
            }
        ],
    )
    card = page.locator(".tool-card")
    code = card.get_by_role("region", name="Tool details")
    assert page.evaluate("window.toolTokenizations") == 0
    expect(card.locator(".token")).to_have_count(0)
    card.locator("summary").click()
    expect(code.locator(".token.keyword").first).to_have_text("import")
    expect(code.locator(".token.comment")).to_have_text("# Read only the synthetic fixture")
    assert code.text_content() == COMMAND
    assert code.locator("script").count() == 0
    token = code.locator(".token.keyword").first.element_handle()
    calls = page.evaluate("window.toolTokenizations")
    code.focus()
    code.evaluate("el => el.scrollLeft = 30")
    scroll = code.evaluate("el => el.scrollLeft")
    page.evaluate(
        """async sid => {
      const {update} = await import('/static/store.js');
      for (let i = 0; i < 10; i++) {
        update({lives: {[sid]: {id:'local-stream', text:'Delta '.repeat(i + 1),
          tools:[], status:'running', userText:'', baseHistoryLength:0}}});
        await new Promise(requestAnimationFrame);
      }
    }""",
        sid,
    )
    expect(code).to_be_focused()
    assert code.evaluate("el => el.scrollLeft") == scroll
    card.locator("summary").click()
    card.locator("summary").click()
    expect(code).to_be_visible()
    assert token.evaluate("el => el.isConnected")
    assert page.evaluate("window.toolTokenizations") == calls


def test_tool_results_wait_until_complete_and_preserve_literal_text(page):
    result = page.evaluate("""async () => {
      const {html, render} = await import('/static/lib.js');
      const {ToolCode} = await import('/static/tool-code.js');
      const root = document.createElement('div'); document.body.append(root);
      const text = JSON.stringify(
        {ok:true, message:'<img src=x onerror=alert(1)>\\u00a0'}, null, 2);
      try {
        const show = (text, enabled) => render(html`<${ToolCode} text=${text}
          label="Tool result" enabled=${enabled} />`, root);
        for(let end=1; end<=text.length; end++) show(text.slice(0,end), false);
        const before = root.querySelectorAll('.token').length;
        show(text, true);
        return {before, after:root.querySelectorAll('.token').length,
          literal:root.textContent === text, injected:root.querySelectorAll('img').length};
      } finally {render(null,root);root.remove();}
    }""")
    assert result["before"] == 0 and result["after"] > 0
    assert result["literal"] and result["injected"] == 0


def test_live_result_coloring_waits_for_tool_completion(page, live_app):
    sid = open_notes(page, live_app, [])
    page.evaluate(
        """async sid => {
          const {update} = await import('/static/store.js');
          window.showToolResult = (output, status) => update({lives:{[sid]:{
            id:'synthetic-run', status:'running', userText:'Fixture request',
            text:'', baseHistoryLength:0, tools:[{
              id:'tool', name:'terminal', preview:'echo ready', output, status,
            }],
          }}});
          window.showToolResult('{', 'running');
        }""",
        sid,
    )
    card = page.locator(".tool-card")
    card.locator("summary").click()
    result = card.get_by_role("region", name="Tool result")
    expect(result).to_be_visible()
    node = result.element_handle()
    page.evaluate("""async () => {
      const output = '{"ok":true,"items":[1,2]}';
      for(let end=2;end<=output.length;end++) {
        window.showToolResult(output.slice(0,end), 'running');
        await new Promise(requestAnimationFrame);
      }
    }""")
    expect(result.locator(".token")).to_have_count(0)
    page.evaluate("window.showToolResult('{\"ok\":true,\"items\":[1,2]}', 'completed')")
    expect(result.locator(".token.boolean")).to_have_text("true")
    assert node.evaluate("el => el.isConnected")
    assert json.loads(result.text_content()) == {"ok": True, "items": [1, 2]}


def test_large_unknown_and_unavailable_tool_highlighting_remain_plain(page):
    result = page.evaluate("""async () => {
      const {html, render} = await import('/static/lib.js');
      const {ToolCode} = await import('/static/tool-code.js');
      const root = document.createElement('div'); document.body.append(root);
      const prism = window.Prism, tokenize = prism.tokenize;
      const large = 'echo "literal <code>"\\r\\n'.repeat(600);
      let calls = 0;
      prism.tokenize = (...args) => {calls++; return tokenize(...args);};
      function show(text, shell) {
        render(null, root);
        render(html`<${ToolCode} text=${text} shell=${shell} enabled=${true} />`,root);
        return root.textContent === text && root.querySelectorAll('.token').length === 0;
      }
      try {
        const bounded = show(large, true);
        const unknown = show('Ordinary tool output, not a programming language.', false);
        const zeroCalls = calls === 0;
        const dense = show(JSON.stringify(Array(400).fill(0)), false);
        window.Prism = undefined;
        const unavailable = show('echo "hello"', true);
        window.Prism = prism;
        prism.tokenize = () => {throw new Error('Synthetic highlighting failure');};
        const failed = show('echo "still readable"', true);
        return {bounded, unknown, zeroCalls, dense, unavailable, failed};
      } finally {window.Prism=prism;prism.tokenize=tokenize;render(null,root);root.remove();}
    }""")
    assert all(result.values()), result
