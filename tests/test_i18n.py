"""Browser locale behavior and live UI changes, using tiny synthetic catalogs."""

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from talaria.app import create_app
from talaria.auth import hash_password
from talaria.config import Settings

from .conftest import capture_browser_errors, serve
from .fake_hermes import KEY
from .test_browser import screenshot
from .test_conversation_features import seed

CATALOG_ROUTE = "**/static/locales/*.json"


@pytest.mark.parametrize("locale", ["en", "zh-Hans", "es", "pt-BR", "fr", "de", "ja"])
def test_shipped_languages_cover_login_and_mobile_settings(browser, live_app, locale):
    catalog = json.loads(
        (Path(__file__).parents[1] / f"src/talaria/static/locales/{locale}.json").read_text()
    )
    context = browser.new_context(locale=locale, viewport={"width": 320, "height": 740})
    errors = capture_browser_errors(context)
    requested = []
    context.on(
        "request",
        lambda request: (
            requested.append(request.url) if "/static/locales/" in request.url else None
        ),
    )
    try:
        page = context.new_page()
        page.goto(live_app[0])
        expect(page.locator("html")).to_have_attribute("lang", locale)
        expect(page.locator("html")).to_have_attribute("dir", "ltr")
        password = page.get_by_label(catalog["Password"], exact=True)
        expect(password).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot(page, f"i18n-{locale}-login")
        password.fill("test-password")
        page.get_by_role("button", name=catalog["Sign in"], exact=True).click()
        expect(page.locator(".composer-input textarea")).to_be_visible()
        page.get_by_role("button", name=catalog["Open sidebar"], exact=True).click()
        page.locator(".sidebar-footer").get_by_role("button", name=catalog["Settings"]).click()
        page.get_by_role("tab", name=catalog["Appearance"], exact=True).click()
        selector = page.get_by_role("combobox", name=catalog["Language"], exact=True)
        expect(selector).to_be_visible()
        assert page.evaluate("""() => {
            const dialog = document.querySelector('dialog[open]');
            const nav = dialog.querySelector('.settings-navigation').getBoundingClientRect();
            return dialog.getBoundingClientRect().width <= innerWidth &&
                dialog.scrollWidth <= dialog.clientWidth &&
                [...dialog.querySelectorAll('[role=tab]')].every(tab => {
                    const box = tab.getBoundingClientRect();
                    return box.left >= nav.left - 0.5 && box.right <= nav.right + 0.5;
                });
        }""")
        screenshot(page, f"i18n-{locale}-settings")
        selector.select_option("en")
        expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_visible()
        page.locator(".language-control select").select_option(locale)
        expect(page.locator("html")).to_have_attribute("lang", locale)
        assert len(requested) == (0 if locale == "en" else 1)
        page.reload()
        expect(page.locator("html")).to_have_attribute("lang", locale)
        expect(page.locator(".composer-input textarea")).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


@pytest.fixture
def language_page(browser, module_server):
    """Import the real module without app startup choosing a language first."""
    contexts = []

    def make(*, locale="en-US", saved=None, blocked_storage=False):
        context = browser.new_context(locale=locale)
        errors = capture_browser_errors(context)
        contexts.append((context, errors))
        if saved is not None:
            context.add_init_script(
                "try { localStorage.setItem('talaria.language', "
                + json.dumps(saved)
                + "); } catch (_) {}"
            )
        if blocked_storage:
            context.add_init_script("""Object.defineProperty(window, 'localStorage', {
                get() { throw new DOMException('Storage blocked', 'SecurityError'); },
            });""")
        page = context.new_page()
        address = module_server + "/__i18n_test__"
        page.route(
            address,
            lambda route: route.fulfill(
                content_type="text/html", body="<!doctype html><html><body></body></html>"
            ),
        )
        page.goto(address)
        page.evaluate("async () => { window.i18n = await import('/static/i18n.js'); }")
        return page

    yield make
    for context, errors in contexts:
        try:
            for page in context.pages:
                page.unroute_all(behavior="wait")
            assert not errors, f"Unhandled browser errors: {errors}"
        finally:
            context.close()


def catalogs(page, data):
    requested = []

    def respond(route):
        locale = urlparse(route.request.url).path.rsplit("/", 1)[-1].removesuffix(".json")
        requested.append(locale)
        if locale in data:
            route.fulfill(json=data[locale])
        else:
            route.fulfill(status=404, body="Missing test catalog")

    page.route(CATALOG_ROUTE, respond)
    return requested


def test_browser_language_matching_respects_scripts_and_invalid_hints(language_page):
    page = language_page()
    cases = [
        (["zh-CN"], "zh-Hans"),
        (["zh-SG"], "zh-Hans"),
        (["zh-Hans-TW"], "zh-Hans"),
        (["zh-TW"], "en"),
        (["zh-Hant", "ja-JP"], "ja"),
        (["invalid_locale", "--", "de-AT"], "de"),
        (["", None, "fr-CA"], "fr"),
        (["pt-PT"], "pt-BR"),
        (["xx-ZZ", "en-GB"], "en"),
        ([], "en"),
    ]
    for hints, expected in cases:
        assert page.evaluate("hints => i18n.resolveLanguage('auto', hints)", hints) == expected
    assert page.evaluate("i18n.resolveLanguage('de', ['ja-JP'])") == "de"
    assert page.evaluate("i18n.resolveLanguage('bad_tag')") == "en"


@pytest.mark.parametrize(
    ("browser_locale", "language"),
    [("en-IN", "en"), ("de-AT", "de")],
)
def test_system_language_keeps_regional_number_and_date_conventions(
    language_page, browser_locale, language
):
    page = language_page(locale=browser_locale)
    catalogs(page, {language: {}})
    result = page.evaluate("""async () => {
        await i18n.initializeLanguage();
        const date = new Date('2026-09-12T12:00:00Z');
        const options = {dateStyle:'short', timeZone:'UTC'};
        return {locale:document.documentElement.lang,
            number:i18n.formatNumber(1234567.89), date:i18n.formatDate(date, options),
            nativeNumber:new Intl.NumberFormat(navigator.language).format(1234567.89),
            nativeDate:new Intl.DateTimeFormat(navigator.language, options).format(date)};
    }""")
    assert result["locale"] == language
    assert result["number"] == result["nativeNumber"]
    assert result["date"] == result["nativeDate"]


def test_english_is_lazy_and_only_selected_catalogs_are_downloaded(language_page):
    page = language_page()
    requested = catalogs(page, {"de": {"Settings": "Einstellungen"}})
    page.evaluate("i18n.initializeLanguage()")
    assert page.evaluate("i18n.t('Settings')") == "Settings"
    assert page.evaluate("i18n.formatNumber(1234.5)") == "1,234.5"
    assert requested == []
    page.evaluate("i18n.setLanguage('de')")
    assert page.evaluate("i18n.t('Settings')") == "Einstellungen"
    assert page.evaluate("i18n.formatNumber(1234.5)") == "1.234,5"
    page.evaluate("i18n.setLanguage('en')")
    assert page.evaluate("i18n.formatNumber(1234.5)") == "1,234.5"
    page.evaluate("i18n.setLanguage('de')")
    assert requested == ["de"]
    assert page.evaluate("localStorage.getItem('talaria.language')") == "de"


@pytest.mark.parametrize(
    ("status", "body"),
    [(404, "Missing"), (200, "{"), (200, "[]")],
    ids=["missing", "malformed-json", "invalid-shape"],
)
def test_unavailable_startup_catalog_falls_back_to_english(language_page, status, body):
    page = language_page(locale="de-DE")
    page.route(
        CATALOG_ROUTE,
        lambda route: route.fulfill(status=status, content_type="application/json", body=body),
    )
    page.evaluate("i18n.initializeLanguage()")
    assert page.evaluate("i18n.t('Sign in')") == "Sign in"
    expect(page.locator("html")).to_have_attribute("lang", "en")
    # A failed fetch is retryable, rather than poisoning the catalog cache.
    page.unroute(CATALOG_ROUTE)
    requested = catalogs(page, {"de": {"Sign in": "Anmelden"}})
    page.evaluate("i18n.setLanguage('de')")
    assert page.evaluate("i18n.t('Sign in')") == "Anmelden"
    assert requested == ["de"]


@pytest.mark.parametrize("newer_fails", [True, False], ids=["failed-newer", "ready-newer"])
def test_superseded_startup_catalog_cannot_leave_login_waiting(browser, live_app, newer_fails):
    context = browser.new_context(locale="de-DE")
    errors = capture_browser_errors(context)
    pending = []
    try:
        page = context.new_page()

        def respond(route):
            if route.request.url.endswith("/de.json"):
                pending.append(route)
            elif newer_fails:
                route.fulfill(status=503, body="Unavailable")
            else:
                route.fulfill(json={"Sign in": "Connexion"})

        page.route(CATALOG_ROUTE, respond)
        with page.expect_request("**/de.json"):
            page.goto(live_app[0])
        expect(page.locator(".initial-loader")).to_be_visible()
        with page.expect_response("**/fr.json"):
            page.evaluate("""async () => {
                const i18n = await import('/static/i18n.js');
                window.initialLanguage = i18n.initializeLanguage();
                localStorage.setItem('talaria.language', 'fr');
                dispatchEvent(new StorageEvent('storage', {key:'talaria.language'}));
            }""")
        if not newer_fails:
            expect(page.locator("html")).to_have_attribute("lang", "fr")
        assert pending
        pending[0].fulfill(json={"Sign in": "Anmelden"})
        page.evaluate("window.initialLanguage")
        expect(
            page.get_by_role("button", name="Sign in" if newer_fails else "Connexion")
        ).to_be_visible()
        expect(page.locator(".initial-loader")).to_have_count(0)
        expect(page.locator("html")).to_have_attribute("lang", "en" if newer_fails else "fr")
        assert not errors, errors
    finally:
        context.close()


def test_missing_and_placeholder_broken_entries_fall_back_individually(language_page):
    page = language_page()
    catalogs(
        page,
        {
            "de": {
                "Settings": "Einstellungen",
                "Hello {name}": "Hallo {person}",
                "Count {count}": "Anzahl",
                "Save": "Speichern {unexpected}",
                "{count} item": {"one": "{count} Ding", "other": "{wrong} Dinge"},
            }
        },
    )
    page.evaluate("i18n.setLanguage('de')")
    assert page.evaluate("i18n.t('Settings')") == "Einstellungen"
    assert page.evaluate("i18n.t('Missing key')") == "Missing key"
    assert page.evaluate("i18n.t('Hello {name}', {name:'Ada'})") == "Hello Ada"
    assert page.evaluate("i18n.t('Count {count}', {count:7})") == "Count 7"
    assert page.evaluate("i18n.t('Save')") == "Save"
    assert page.evaluate("i18n.n('{count} item', '{count} items', 2)") == "2 items"
    assert page.evaluate("i18n.t('constructor')") == "constructor"


@pytest.mark.parametrize(
    ("locale", "forms", "expected"),
    [
        (
            "fr",
            {"one": "{count} élément", "other": "{count} éléments"},
            ["0 élément", "1 élément", "2 éléments"],
        ),
        ("zh-Hans", {"other": "{count} 项"}, ["0 项", "1 项", "2 项"]),
    ],
)
def test_catalog_plurals_follow_the_selected_language(language_page, locale, forms, expected):
    page = language_page()
    catalogs(page, {locale: {"{count} item": forms}})
    page.evaluate("locale => i18n.setLanguage(locale)", locale)
    assert (
        page.evaluate("[0, 1, 2].map(n => i18n.n('{count} item', '{count} items', n))") == expected
    )
    # Missing entries remain grammatical English, including French's zero category.
    assert page.evaluate("[0, 1, 2].map(n => i18n.n('{count} file', '{count} files', n))") == [
        "0 files",
        "1 file",
        "2 files",
    ]


def test_named_and_rich_interpolation_keep_catalog_and_values_inert(language_page):
    page = language_page()
    payload = '<img src=x onerror="window.injected=true"><script>window.injected=true</script>'
    catalogs(
        page,
        {
            "de": {
                "Hello {name}": "Hallo {name}",
                "Read {link} now.": payload + " Danach {link}.",
            }
        },
    )
    page.evaluate(
        """async payload => {
            await i18n.setLanguage('de');
            const {html, render} = await import('/static/lib.js');
            const link = html`<a href="#synthetic" onClick=${() => window.linkClicks = 1}>
                Native link text</a>`;
            render(html`<main>
                <p id="named">${i18n.t('Hello {name}', {name:payload + ' {link}'})}</p>
                <p id="rich">${i18n.rich('Read {link} now.', {link})}</p>
            </main>`, document.body);
        }""",
        payload,
    )
    expect(page.locator("#named")).to_have_text("Hallo " + payload + " {link}")
    expect(page.locator("#rich")).to_contain_text(payload + " Danach")
    expect(page.locator("main img, main script")).to_have_count(0)
    page.get_by_role("link", name="Native link text").click()
    assert page.evaluate("window.linkClicks") == 1
    assert page.evaluate("window.injected === undefined")


def test_slow_previous_selection_cannot_overwrite_a_newer_language(language_page):
    page = language_page()
    pending = []

    def respond(route):
        if route.request.url.endswith("/de.json"):
            pending.append(route)
        else:
            route.fulfill(json={"Settings": "Paramètres"})

    page.route(CATALOG_ROUTE, respond)
    with page.expect_request("**/de.json"):
        page.evaluate("() => { window.previousLanguage = i18n.setLanguage('de'); }")
    page.evaluate("i18n.setLanguage('fr')")
    assert page.evaluate("i18n.t('Settings')") == "Paramètres"
    assert len(pending) == 1
    pending[0].fulfill(json={"Settings": "Einstellungen"})
    page.evaluate("window.previousLanguage")
    expect(page.locator("html")).to_have_attribute("lang", "fr")
    assert page.evaluate("localStorage.getItem('talaria.language')") == "fr"
    assert page.evaluate("i18n.t('Settings')") == "Paramètres"


def test_blocked_storage_does_not_prevent_startup_or_live_selection(language_page):
    page = language_page(locale="fr-CA", blocked_storage=True)
    catalogs(page, {"fr": {"Settings": "Paramètres"}, "de": {"Settings": "Einstellungen"}})
    page.evaluate("i18n.initializeLanguage()")
    assert page.evaluate("i18n.t('Settings')") == "Paramètres"
    page.evaluate("i18n.setLanguage('de')")
    assert page.evaluate("i18n.t('Settings')") == "Einstellungen"
    expect(page.locator("html")).to_have_attribute("lang", "de")


@pytest.mark.parametrize("saved", ["bad_tag", "zh-TW", "constructor"])
def test_invalid_saved_preference_uses_the_browser_language(language_page, saved):
    page = language_page(locale="fr-FR", saved=saved)
    requested = catalogs(page, {"fr": {"Settings": "Paramètres"}})
    page.evaluate("i18n.initializeLanguage()")
    expect(page.locator("html")).to_have_attribute("lang", "fr")
    assert requested == ["fr"]


def open_appearance(page, mobile=False):
    if mobile:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Appearance", exact=True).click()


def test_language_selector_keeps_english_on_failed_load_and_allows_retry(page):
    page.route(CATALOG_ROUTE, lambda route: route.fulfill(status=404))
    open_appearance(page)
    select = page.get_by_role("combobox", name="Language", exact=True)
    select.select_option("de")
    expect(page.get_by_role("alert")).to_have_text(
        "Could not load this language. Please try again."
    )
    expect(select).to_be_enabled()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_visible()
    page.unroute(CATALOG_ROUTE)
    catalogs(page, {"de": {"Settings": "Einstellungen", "Language": "Sprache"}})
    select.select_option("de")
    expect(page.get_by_role("dialog", name="Einstellungen", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Sprache", exact=True)).to_have_value("de")
    expect(page.get_by_role("alert")).to_have_count(0)


def test_language_selector_retains_keyboard_focus_after_loading(page):
    catalogs(page, {"de": {"Language": "Sprache"}})
    open_appearance(page)
    select = page.get_by_role("combobox", name="Language", exact=True)
    select.focus()
    expect(select).to_be_focused()
    select.select_option("de")
    expect(page.get_by_role("combobox", name="Sprache", exact=True)).to_be_focused()


@pytest.mark.parametrize("previous_fails", [False, True], ids=["old-success", "old-failure"])
def test_language_selector_can_change_a_pending_choice(page, previous_fails):
    pending = []

    def respond(route):
        if route.request.url.endswith("/de.json"):
            pending.append(route)
        else:
            route.fulfill(json={"Language": "Langue", "Settings": "Paramètres"})

    page.route(CATALOG_ROUTE, respond)
    open_appearance(page)
    select = page.locator(".language-control select")
    select.focus()
    with page.expect_request("**/de.json"):
        select.select_option("de")
    expect(select).to_have_attribute("aria-busy", "true")
    expect(select).to_have_value("de")
    expect(select).to_be_enabled()
    expect(select).to_be_focused()
    select.select_option("fr")
    expect(page.get_by_role("dialog", name="Paramètres", exact=True)).to_be_visible()
    expect(select).to_have_attribute("aria-busy", "false")
    expect(select).to_have_value("fr")
    with page.expect_response("**/de.json"):
        if previous_fails:
            pending[0].fulfill(status=503)
        else:
            pending[0].fulfill(json={"Language": "Sprache", "Settings": "Einstellungen"})
    page.evaluate("""() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
    expect(page.locator("html")).to_have_attribute("lang", "fr")
    expect(select).to_have_value("fr")
    expect(select).to_be_focused()
    expect(page.get_by_role("alert")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('talaria.language')") == "fr"


@pytest.mark.parametrize("streaming", [False, True], ids=["saved", "streaming"])
def test_markdown_copy_feedback_changes_language_without_rewriting_code(language_page, streaming):
    page = language_page()
    catalogs(
        page,
        {
            "de": {
                "Copy code": "Code kopieren",
                "Copied": "Kopiert",
                "{language} code": "{language}-Code",
            }
        },
    )
    page.evaluate(
        """async streaming => {
        await i18n.initializeLanguage();
        const {html, render} = await import('/static/lib.js');
        const {Markdown} = await import('/static/markdown.js');
        window.drawCode = text => render(html`<${Markdown}
            text=${text} streaming=${streaming} />`, document.body);
        Object.defineProperty(navigator, 'clipboard', {value:{
            writeText:async text => { window.copiedCode = text; },
        }});
        drawCode('```native-id\\nsource remains unchanged' + (streaming ? '' : '\\n```'));
    }""",
        streaming,
    )
    button = page.locator("[data-copy-code]")
    expect(button).to_have_text("Copy code")
    button.click()
    expect(button).to_have_text("Copied")
    page.evaluate("""async () => {
        const {marked} = await import('/static/vendor/marked.js');
        const lex = marked.Lexer.prototype.lex;
        window.copyParses = 0;
        marked.Lexer.prototype.lex = function(...args) {
            window.copyParses++;
            return lex.apply(this, args);
        };
        window.originalCode = document.querySelector('pre code');
        window.originalCopy = document.querySelector('[data-copy-code]');
        await i18n.setLanguage('de');
    }""")
    expect(page.locator("[data-copy-code]")).to_have_text("Kopiert")
    expect(page.locator("pre")).to_have_attribute("aria-label", "native-id-Code")
    assert page.evaluate("copyParses") == 0
    if streaming:
        page.evaluate("drawCode('```native-id\\nsource remains unchanged\\nanother native line')")
        expect(page.locator("pre code")).to_have_text(
            "source remains unchanged\nanother native line"
        )
        expect(page.locator("[data-copy-code]")).to_have_text("Kopiert")
    assert page.evaluate("""originalCode === document.querySelector('pre code') &&
        originalCopy === document.querySelector('[data-copy-code]')""")
    assert page.evaluate("copiedCode") == "source remains unchanged"


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
def test_live_selector_preserves_draft_saved_nodes_and_reader_state(page, live_app, mobile):
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    sid = seed(live_app[1])
    code = "\n".join(f"native source {i}: " + "unchanged " * 20 for i in range(35))
    live_app[1].messages[sid] = [
        {"id": 1, "role": "user", "content": "Native user text"},
        {
            "id": 2,
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "native-call",
                    "function": {"name": "terminal", "arguments": '{"command":"echo stable"}'},
                }
            ],
        },
        {
            "id": 3,
            "role": "tool",
            "tool_call_id": "native-call",
            "tool_name": "terminal",
            "content": "Native tool output",
        },
        {"id": 4, "role": "assistant", "content": "Saved prose\n\n```text\n" + code + "\n```"},
    ]
    requested = catalogs(
        page,
        {
            "de": {
                "Settings": "Einstellungen",
                "Language": "Sprache",
                "Appearance": "Darstellung",
                "Close dialog": "Dialog schließen",
                "Message Hermes": "Nachricht an Hermes",
                "Your message": "Deine Nachricht",
                "{name} response": "Antwort von {name}",
                "Copy code": "Code kopieren",
                "{language} code": "{language}-Code",
                "Tool details": "Werkzeugdetails",
                "Tool result": "Werkzeugergebnis",
                "Finished": "Beendet",
                "Result": "Ergebnis",
                "Design notes": "DO NOT TRANSLATE",
                "Native user text": "DO NOT TRANSLATE",
                "Native tool output": "DO NOT TRANSLATE",
                "Saved prose": "DO NOT TRANSLATE",
            }
        },
    )
    page.reload()
    page.evaluate("sid => import('/static/store.js').then(s => s.openSession(sid))", sid)
    expect(page.locator(".code-block code")).to_have_text(code)
    page.get_by_label("Message Hermes", exact=True).fill("Unsent draft {name} 简体中文")
    page.locator(".tool-card summary").click()
    expect(page.locator(".tool-card")).to_have_attribute("open", "")
    open_appearance(page, mobile)
    page.get_by_role("combobox", name="Language", exact=True).focus()
    page.evaluate("""async () => {
        const {marked} = await import('/static/vendor/marked.js');
        const lex = marked.Lexer.prototype.lex;
        window.localeParses = 0;
        marked.Lexer.prototype.lex = function(...args) {
            window.localeParses++;
            return lex.apply(this, args);
        };
        const article = document.querySelector('.message.assistant');
        const code = article.querySelector('.code-block code');
        const pre = code.parentElement;
        const viewport = document.querySelector('.conversation-viewport');
        pre.scrollLeft = 80;
        viewport.scrollTop = 150;
        viewport.dispatchEvent(new Event('scroll'));
        const range = document.createRange();
        range.setStart(code.firstChild, 0);
        range.setEnd(code.firstChild, 15);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
        // The modal hides the inert conversation from Selection.toString(),
        // but the selection's live Range must still retain its text and nodes.
        window.localeReader = {article, code, pre, viewport,
            tool:article.querySelector('.tool-card'),
            copy:article.querySelector('[data-copy-code]'),
            selection:getSelection().rangeCount ? getSelection().getRangeAt(0).toString() : '',
            left:pre.scrollLeft, top:viewport.scrollTop};
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    before = page.evaluate(
        "({left:localeReader.left, top:localeReader.top, selection:localeReader.selection})"
    )
    assert before["left"] > 0 and before["top"] > 0 and before["selection"]
    page.get_by_role("combobox", name="Language", exact=True).select_option("de")
    expect(page.get_by_role("dialog", name="Einstellungen", exact=True)).to_be_visible()
    expect(page.locator(".code-block pre")).to_have_attribute("aria-label", "text-Code")
    expect(page.locator(".code-heading button")).to_have_text("Code kopieren")
    expect(page.locator(".tool-status .sr-only")).to_have_text("Beendet")
    expect(page.locator(".message.assistant")).to_have_attribute("aria-label", "Antwort von Hermes")
    retained = page.evaluate("""() => ({
        article:localeReader.article === document.querySelector('.message.assistant'),
        code:localeReader.code === document.querySelector('.code-block code'),
        copy:localeReader.copy === document.querySelector('[data-copy-code]'),
        tool:localeReader.tool === document.querySelector('.tool-card') && localeReader.tool.open,
        selection:getSelection().rangeCount ? getSelection().getRangeAt(0).toString() : '',
        left:localeReader.pre.scrollLeft,
        top:localeReader.viewport.scrollTop, parses:localeParses,
    })""")
    assert retained == {
        "article": True,
        "code": True,
        "copy": True,
        "tool": True,
        **before,
        "parses": 0,
    }
    screenshot(page, f"i18n-{'mobile' if mobile else 'desktop'}-settings")
    page.get_by_role("button", name="Dialog schließen", exact=True).click()
    if mobile:
        page.get_by_role("button", name="Close sidebar", exact=True).first.click()
    expect(page.locator(".composer-input textarea")).to_have_value("Unsent draft {name} 简体中文")
    expect(page.locator(".topbar-title")).to_have_text("Design notes")
    expect(page.locator(".user-content")).to_have_text("Native user text")
    expect(page.locator(".tool-content")).to_contain_text("Native tool output")
    expect(page.locator(".code-block code")).to_have_text(code)
    assert requested == ["de"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, f"i18n-{'mobile' if mobile else 'desktop'}-conversation")


@pytest.mark.parametrize("strip", [False, True], ids=["preserved-prefix", "stripped-prefix"])
def test_language_catalog_loads_beneath_public_proxy_prefix(browser, live_app, tmp_path, strip):
    prefix = "/apps/talaria"
    settings = Settings(
        hermes_url=live_app[2].state.settings.hermes_url,
        api_key=KEY,
        password_hash=hash_password("test-password"),
        signing_key="test-signing-key",
        public_url="http://127.0.0.1" + prefix,
    )
    app = create_app(settings, tmp_path / "prefixed-config.json")

    async def proxy(scope, receive, send):
        if scope["type"] == "http" and strip and scope["path"].startswith(prefix + "/"):
            scope = dict(scope, path=scope["path"][len(prefix) :])
        await app(scope, receive, send)

    server, thread, address = serve(proxy)
    settings.public_url = address + prefix
    context = browser.new_context(locale="en-US")
    errors = capture_browser_errors(context)
    try:
        page = context.new_page()
        requested = []

        def respond(route):
            requested.append(urlparse(route.request.url).path)
            route.fulfill(json={"Sign in": "Anmelden", "Language": "Sprache"})

        page.route(CATALOG_ROUTE, respond)
        page.goto(address + prefix + "/")
        page.get_by_role("combobox", name="Language", exact=True).select_option("de")
        expect(page.get_by_role("button", name="Anmelden", exact=True)).to_be_visible()
        assert requested == [prefix + "/static/locales/de.json"]
        expect(page.locator("html")).to_have_attribute("lang", "de")
        assert not errors
    finally:
        context.close()
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
