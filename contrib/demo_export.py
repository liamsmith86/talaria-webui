"""Write the public demo as static files for Cloudflare Pages."""

import argparse
import json
import shutil
from pathlib import Path

from talaria.demo import SAMPLES, bootstrap_data, create_demo
from talaria.web import ENCODINGS, IMMUTABLE, ROBOTS, SECURITY_HEADERS, STATIC

CORS = "; crossorigin"
API_FILES = ("bootstrap", "samples")
# What the first paint needs, as (path in the build, destination, fetched with CORS).
PRELOADS = (
    ("appearance.js", "script", False),
    ("styles.css", "style", False),
    ("styles/demo.css", "style", False),
    ("boot.js", "script", True),
    ("app.js", "script", True),
    ("vendor/inter.woff2", "font", True),
)


def copy_build(target):
    """The files the server's production asset mount serves, built output first."""
    for root in (STATIC / "built", STATIC):
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            destination = target / relative
            if (
                path.is_file()
                and path.suffix not in ENCODINGS.values()
                and relative.parts[0] != "built"
                and relative != Path("index.html")
                and not destination.exists()
            ):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)


def headers(build):
    site = {
        **SECURITY_HEADERS,
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
    }
    assets = "".join(
        f"  Link: </{build}{path}>; rel=preload; as={kind}{CORS * cors}\n"
        for path, kind, cors in PRELOADS
    )
    # These are only requested once the page has run, so preloading saves whole round trips.
    data = "".join(f"  Link: </api/{name}>; rel=preload; as=fetch{CORS}\n" for name in API_FILES)
    return (
        "/*\n"
        + "".join(f"  {name}: {value}\n" for name, value in site.items())
        # The demo's files are for this site only, so drop the wildcard Pages adds.
        + "  ! Access-Control-Allow-Origin\n"
        + f"/\n{assets}{data}"
        + "/api/*\n  Content-Type: application/json\n"
        + f"/{build}*\n  Cache-Control: {IMMUTABLE}\n"
    )


def export(site):
    app = create_demo(production=True)
    build = app.state.asset_path
    site.mkdir(parents=True)
    copy_build(site / build)
    # The manifest keeps its stable path, and its icons are relative to it.
    shutil.copy2(STATIC / "app.webmanifest", site / "static/app.webmanifest")
    shutil.copytree(STATIC / "icons", site / "static/icons")
    (site / "api").mkdir()
    (site / "api/bootstrap").write_text(json.dumps(bootstrap_data()))
    shutil.copy2(SAMPLES, site / "api/samples")
    (site / "index.html").write_text(app.state.index_html.replace("__TALARIA_BASE__", "/"))
    (site / "robots.txt").write_text(ROBOTS)
    (site / "_headers").write_text(headers(build))
    # Without this, Pages answers unknown paths with the application page.
    (site / "404.html").write_text(
        '<!doctype html><html lang="en"><title>Not found</title><h1>Not found</h1></html>\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path, help="Directory to create")
    export(parser.parse_args().site)


if __name__ == "__main__":
    main()
