"""The browser gate detects real uncaught errors across document lifetimes."""

from .conftest import capture_browser_errors


def test_error_capture_survives_navigation_and_ignores_caught_fetches(browser, live_app):
    context = browser.new_context()
    errors = capture_browser_errors(context)
    try:
        page = context.new_page()
        page.route(
            "**/error-probe",
            lambda route: route.fulfill(
                body="<!doctype html><title>Error capture probe</title>", content_type="text/html"
            ),
        )
        # Count acknowledged binding calls, so assertions need no timing sleeps.
        context.add_init_script("""(() => {
          window.reportedErrors = 0;
          const report = window.__talariaReportError;
          window.__talariaReportError = async error => {
            await report(error);
            window.reportedErrors++;
          };
        })()""")
        address = live_app[0] + "/error-probe"
        for _ in range(2):
            page.goto(address)
            page.evaluate("""() => {
              setTimeout(() => { throw new Error('Uncaught probe exception'); }, 0);
              Promise.reject(new Error('Unhandled probe rejection'));
            }""")
            page.wait_for_function("window.reportedErrors === 2")
            # A plain, caught same-origin fetch during pagehide causes a WebKit
            # inspector diagnostic; it is not an uncaught application exception.
            page.evaluate("""() => {
              addEventListener('pagehide', () => fetch('/health').catch(() => {}));
            }""")
        page.reload()
        assert len(errors) == 4
        assert sum(error["kind"] == "error" for error in errors) == 2
        assert sum(error["kind"] == "rejection" for error in errors) == 2
        assert all(error["url"] for error in errors)
        assert all(error["url"] == address for error in errors if error["kind"] == "rejection")
        assert all(error["stack"] for error in errors)
    finally:
        context.close()
