"""Incremental rendering must preserve full-document Markdown semantics and DOM state."""

from playwright.sync_api import expect


def test_streamed_markdown_matches_full_parse_at_every_prefix(page):
    result = page.evaluate(r"""async () => {
      const {html, render} = await import('/static/lib.js');
      const {Markdown, renderMarkdown} = await import('/static/markdown.js');
      const root = document.createElement('div');
      document.body.append(root);
      const prism = window.Prism; window.Prism = undefined;
      const cases = [
        'A heading\n=========\n\nParagraph with **bold**, _emphasis_, and `code`.\n\n---\n\nEnd',
        '- One\n  - Nested\n\n    Paragraph\n- Two\n\n> Quote\n>\n> More\n\nAfter',
        '[Later][ref] and ![Image][img]\n\n[ref]: https://example.com "Title"\n' +
          '[img]: https://example.com/a.png\n',
        '| A | B |\n|---|---|\n| one | two |\n\n```javascript\nconst x = "<b>";\n```\n\nAfter',
        '<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n' +
          '<div onclick="bad()">Raw HTML</div>\n',
        'First\r\n\r\nSecond\r\n\r\n    indented\r\n\r\nEnd',
        'First\n\nSecond\n\nThird\n\n  10. One\n  11. Two\n  12. Three\n\n    Nested\n',
        '| A | B |\n|:-|--:|\n| `one` | **two** |\n| x\\|y | z |\n' +
          '| [link][r] | &amp; |\n\n[r]: /safe\n',
        '- One\n- Two\n- Three\n\n  More in the third item\n\nAfter\n\n---\n',
        'A sentence. More words! www.example.com and 1. item\n\n```text\nA\n\nB\n```\n',
        'Intro\n\n| A | B |\n|---|---|\n| one | two |\n| three | four |\n  \nAfter',
      ];
      function actual() {
        return [...root.querySelector('.markdown').children].map(el => el.innerHTML).join('');
      }
      const normalize = value => value.replace(/>\s+</g, '><').trim();
      let checked = 0;
      try {
        for (const source of cases) {
          render(null, root);
          for (let i=0; i<=source.length; i++) {
            const text = source.slice(0,i);
            // Completed output must be identical to the existing sanitized parser.
            render(html`<${Markdown} text=${text} streaming=${true} />`, root);
            if(normalize(actual()) !== normalize(renderMarkdown(text, false)))
              return {phase:'streaming', source, i, actual:actual(),
                expected:renderMarkdown(text, false)};
            checked++;
          }
          render(html`<${Markdown} text=${source} streaming=${false} />`, root);
          if(normalize(actual()) !== normalize(renderMarkdown(source)))
            return {phase:'complete', source, actual:actual(), expected:renderMarkdown(source)};
        }
        return {checked};
      } finally { window.Prism = prism; render(null,root); root.remove(); }
    }""")
    assert result.get("checked", 0) > 400, result


def test_incremental_scanning_bounds_work_for_long_replies(page):
    result = page.evaluate(r"""async () => {
      const {MarkdownStream}=await import('/static/markdown-stream.js');
      const {marked}=await import('/static/vendor/marked.js');
      const original=marked.Lexer.prototype.lex;
      let scanned=0;
      marked.Lexer.prototype.lex=function(text,...args) {
        scanned+=text.length; return original.call(this,text,...args);
      };
      try {
        return [
          'A **useful** paragraph.\n\n'.repeat(200),
          'A useful paragraph. More detail! '.repeat(200),
          '```text\n'+'A line of code\n'.repeat(300),
          '| A | B |\n|---|---|\n'+'| One | Two |\n'.repeat(300),
          '- A **useful** item\n'.repeat(300),
        ].map(source=>{
          scanned=0;
          const scanner=new MarkdownStream();
          for(let end=64;end<source.length;end+=64) scanner.read(source.slice(0,end),true);
          scanner.read(source,false);
          return {characters:source.length,scanned};
        });
      } finally {marked.Lexer.prototype.lex=original;}
    }""")
    for case in result:
        assert case["scanned"] < case["characters"] * 6, case


def test_rows_and_items_keep_nodes_through_appends_and_final_reconciliation(page):
    result = page.evaluate(r"""async () => {
      const {html,render}=await import('/static/lib.js');
      const {Markdown}=await import('/static/markdown.js');
      const root=document.createElement('div');document.body.append(root);
      try {
        const results=[];
        for(const [source,more,selector] of [
          ['| A | B |\n|---|---|\n| One | Two |\n| Three | Four |','\n| Five | Six |','tbody tr'],
          ['- **One**\n- Two\n- Three','\n- Four','li'],
        ]) {
          render(null,root);
          render(html`<${Markdown} text=${source} streaming=${true} />`,root);
          const first=root.querySelector(selector);
          const range=document.createRange();range.selectNodeContents(first);
          getSelection().removeAllRanges();getSelection().addRange(range);
          const selected=getSelection().toString();
          for(let i=1;i<=more.length;i++)
            render(html`<${Markdown} text=${source+more.slice(0,i)} streaming=${true} />`,root);
          render(html`<${Markdown} text=${source+more} streaming=${false} />`,root);
          results.push(first===root.querySelector(selector) &&
            getSelection().toString()===selected);
        }
        return results;
      } finally {getSelection().removeAllRanges();render(null,root);root.remove();}
    }""")
    assert result == [True, True]


def test_completed_blocks_keep_nodes_selection_and_code_scroll_during_stream(page):
    result = page.evaluate(r"""async () => {
      const {html, render} = await import('/static/lib.js');
      const {Markdown} = await import('/static/markdown.js');
      const root = document.createElement('div'); document.body.append(root);
      const prefix = 'A **selectable** paragraph.\n\n```javascript\n' +
        'const greeting = "a very long line";'.repeat(100) + '\n```\n\n';
      try {
        render(html`<${Markdown} text=${prefix+'Next'} streaming=${true} />`,root);
        const paragraph = root.querySelector('p'), code = root.querySelector('code');
        const pre = root.querySelector('pre'); pre.scrollLeft=100;
        const scroll = pre.scrollLeft;
        const range = document.createRange(); range.selectNodeContents(paragraph);
        getSelection().removeAllRanges(); getSelection().addRange(range);
        render(html`<${Markdown}
          text=${prefix+'Next paragraph is growing.'} streaming=${true} />`,root);
        return {sameParagraph:paragraph===root.querySelector('p'),
          sameCode:code===root.querySelector('code'),
          selection:getSelection().toString(), scroll:pre.scrollLeft, previousScroll:scroll};
      } finally { getSelection().removeAllRanges(); render(null,root); root.remove(); }
    }""")
    assert result["sameParagraph"] and result["sameCode"]
    assert result["selection"] == "A selectable paragraph."
    assert result["scroll"] == result["previousScroll"]


def test_stream_tail_defers_highlighting_and_finalizes_without_losing_literal_text(page):
    result = page.evaluate(r"""async () => {
      const {html, render} = await import('/static/lib.js');
      const {Markdown} = await import('/static/markdown.js');
      const root = document.createElement('div'); document.body.append(root);
      const text = '```javascript\nconst answer = 0;\n```';
      try {
        render(html`<${Markdown} text=${text} streaming=${true} />`,root);
        const streamed = root.querySelector('code').textContent;
        const before = root.querySelectorAll('.token').length;
        render(html`<${Markdown} text=${text} streaming=${false} />`,root);
        return {streamed, final:root.querySelector('code').textContent,
          before, after:root.querySelectorAll('.token').length};
      } finally { render(null,root); root.remove(); }
    }""")
    assert result["before"] == 0 and result["after"] > 0
    assert result["streamed"] == result["final"] == "const answer = 0;"


def test_composer_grows_shrinks_and_keeps_a_bounded_height(page):
    field = page.get_by_label("Message Hermes")
    initial = field.bounding_box()["height"]
    field.fill("Line\n" * 40)
    expect(field).to_have_value("Line\n" * 40)
    page.wait_for_function("() => document.querySelector('textarea').clientHeight > 100")
    assert field.bounding_box()["height"] <= 221
    field.fill("Short")
    page.wait_for_function("() => document.querySelector('textarea').clientHeight < 60")
    assert abs(field.bounding_box()["height"] - initial) <= 1


def test_long_inline_groups_preserve_markdown_references_and_existing_nodes(page):
    result = page.evaluate(r"""async () => {
      const {html, render} = await import('/static/lib.js');
      const {Markdown,renderMarkdown} = await import('/static/markdown.js');
      const root=document.createElement('div'); document.body.append(root);
      const text='A **bold** word, `code`, and [a reference][later]. '.repeat(150);
      try {
        render(html`<${Markdown} text=${text} streaming=${true} />`,root);
        const first=root.querySelector('.markdown-inline');
        const changed=text+' More *emphasis*.';
        render(html`<${Markdown} text=${changed} streaming=${true} />`,root);
        const stable=first===root.querySelector('.markdown-inline');
        const completed=changed+'\n\n[later]: https://example.com "Title"';
        render(html`<${Markdown} text=${completed} streaming=${false} />`,root);
        const clone=root.cloneNode(true);
        for(const span of clone.querySelectorAll('.markdown-inline'))
          span.replaceWith(...span.childNodes);
        const actual=[...clone.querySelector('.markdown').children]
          .map(el=>el.innerHTML).join('').replace(/>\s+</g,'><').trim();
        const expected=renderMarkdown(completed).replace(/>\s+</g,'><').trim();
        return {stable, matches:actual===expected,
          links:root.querySelectorAll('a[href="https://example.com"]').length};
      } finally {render(null,root);root.remove();}
    }""")
    assert result == {"stable": True, "matches": True, "links": 150}


def test_large_code_stays_complete_without_expensive_highlighting(page):
    result = page.evaluate(r"""async () => {
      const {renderMarkdown} = await import('/static/markdown.js');
      const source='const greeting = "hello";\n'.repeat(1600);
      const root=document.createElement('div');
      root.innerHTML=renderMarkdown('```javascript\n'+source+'```');
      return {same:root.querySelector('code').textContent===source.trimEnd(),
        tokens:root.querySelectorAll('.token').length,
        copy:!!root.querySelector('[data-copy-code]')};
    }""")
    assert result == {"same": True, "tokens": 0, "copy": True}


def test_deferred_highlighting_preserves_code_focus_and_scroll(page):
    page.evaluate(r"""async () => {
      const {html, render} = await import('/static/lib.js');
      const {Markdown} = await import('/static/markdown.js');
      const original = window.IntersectionObserver;
      window.IntersectionObserver = class {
        constructor(callback) {
          window.highlightVisibleCode = () => callback([{isIntersecting:true}]);
        }
        observe() {}
        disconnect() {}
      };
      const root = document.createElement('div'); document.body.append(root);
      window.cleanupCodeTest = () => {
        render(null,root); root.remove(); window.IntersectionObserver = original;
      };
      const text = '```javascript\nconst answer = "' + 'wide text '.repeat(100) + '";\n```';
      render(html`<${Markdown} text=${text} deferHighlight=${true} />`,root);
      window.originalCodeRegion = root.querySelector('pre');
      originalCodeRegion.focus(); originalCodeRegion.scrollLeft = 100;
    }""")
    try:
        page.wait_for_function("() => !!window.highlightVisibleCode")
        page.evaluate("window.highlightVisibleCode()")
        page.wait_for_function("() => document.querySelector('pre .token')")
        assert page.evaluate("""() => originalCodeRegion.isConnected &&
            document.activeElement === originalCodeRegion &&
            originalCodeRegion.scrollLeft === 100""")
    finally:
        page.evaluate("window.cleanupCodeTest()")
