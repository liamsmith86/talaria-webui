"""Write the public demo as static files for Cloudflare Pages."""

import argparse
import json
import shutil
from pathlib import Path

from talaria.demo import SAMPLES, bootstrap_data, create_demo
from talaria.web import ENCODINGS, IMMUTABLE, SECURITY_HEADERS, STATIC

ORIGIN = "https://fly.withtalaria.com"
API_FILES = ("bootstrap", "samples")
# What the first paint needs, as (path in the build, how to preload it). Module scripts take
# modulepreload, because Firefox doesn't hand a script preload to a module script.
PRELOADS = (
    ("appearance.js", "rel=preload; as=script"),
    ("styles.css", "rel=preload; as=style"),
    ("styles/demo.css", "rel=preload; as=style"),
    ("boot.js", "rel=modulepreload"),
    ("app.js", "rel=modulepreload"),
    ("vendor/inter.woff2", "rel=preload; as=font; crossorigin"),
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
        # Unlike a private installation, the demo should appear in search results.
        **{name: value for name, value in SECURITY_HEADERS.items() if name != "x-robots-tag"},
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
    }
    assets = "".join(f"  Link: </{build}{path}>; {preload}\n" for path, preload in PRELOADS)
    # These are only requested once the page has run, so preloading saves whole round trips.
    data = "".join(
        f"  Link: </api/{name}>; rel=preload; as=fetch; crossorigin\n" for name in API_FILES
    )
    return (
        "/*\n"
        + "".join(f"  {name}: {value}\n" for name, value in site.items())
        # The demo's files are for this site only, so drop the wildcard Pages adds.
        + "  ! Access-Control-Allow-Origin\n"
        + f"/\n  Link: <{ORIGIN}/>; rel=canonical\n{assets}{data}"
        + "/api/*\n  Content-Type: application/json\n"
        + f"/{build}*\n  Cache-Control: {IMMUTABLE}\n"
        # Previews and the default address stay out of search results.
        + "https://:project.pages.dev/*\n  X-Robots-Tag: noindex\n"
        + "https://:version.:project.pages.dev/*\n  X-Robots-Tag: noindex\n"
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
    (site / "robots.txt").write_text("User-agent: *\nAllow: /\n")
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
