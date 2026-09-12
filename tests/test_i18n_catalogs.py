"""Translation gates reject source drift and broken translator substitutions."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.quality


@pytest.fixture
def catalogs(tmp_path):
    (tmp_path / "contrib").mkdir()
    for name in ["i18n.mjs", "i18n.py"]:
        shutil.copyfile(ROOT / "contrib" / name, tmp_path / "contrib" / name)
    (tmp_path / "node_modules").symlink_to(ROOT / "node_modules")
    root = tmp_path / "src/talaria/static/locales"
    root.mkdir(parents=True)
    (root.parent / "i18n.js").write_text("""export const languages = [
      { code: "en" }, { code: "fr" }, { code: "zh-Hans" }
    ];
    t("Open {name}"); rich("Read {guide}", {}); msg("Settings");
    n("{count} session", "{count} sessions", 2);
    """)
    (tmp_path / "src/talaria/backend.py").write_text("""
raise APIError("Connection failed.")
DOWNLOAD_CHANGED = "Download changed."
raise APIError(DOWNLOAD_CHANGED)
raise APIError(f"Invalid {native_diagnostic}")
native_output = "Never translate agent output"
""")
    french = {
        "Open {name}": "Ouvrir {name}",
        "Read {guide}": "Lire {guide}",
        "Settings": "Paramètres",
        "Connection failed.": "Connexion impossible.",
        "Download changed.": "Téléchargement modifié.",
        "{count} session": {
            "one": "{count} session",
            "many": "{count} sessions",
            "other": "{count} sessions",
        },
    }
    chinese = {
        "Open {name}": "打开{name}",
        "Read {guide}": "阅读{guide}",
        "Settings": "设置",
        "Connection failed.": "连接失败。",
        "Download changed.": "下载已更改。",
        "{count} session": {"other": "{count} 个会话"},
    }
    for code, data in [("fr", french), ("zh-Hans", chinese)]:
        (root / f"{code}.json").write_text(json.dumps(data, ensure_ascii=False))
    return tmp_path, root


def run(root, *args):
    return subprocess.run(
        ["node", "contrib/i18n.mjs", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "TALARIA_I18N_PYTHON": sys.executable},
    )


def test_catalogs_follow_source_and_native_plural_rules(catalogs):
    repo, root = catalogs
    assert run(repo, "--extract").returncode == 0
    source = json.loads((root / "en.json").read_text())
    assert set(source) == {
        "Open {name}",
        "Read {guide}",
        "Settings",
        "Connection failed.",
        "Download changed.",
        "{count} session",
    }
    assert run(repo).returncode == 0
    with (root.parent / "i18n.js").open("a") as stream:
        stream.write('t("New control");\n')
    result = run(repo)
    assert result.returncode != 0 and "English catalog is stale" in result.stderr


@pytest.mark.parametrize(
    "defect, diagnostic",
    [
        ("placeholder", "changed placeholders"),
        ("plural", "missing/invalid"),
        ("extra_form", "missing/invalid"),
        ("missing", "missing/invalid"),
        ("unused", "unused"),
        ("duplicate", "duplicate"),
        ("blank", "missing/invalid"),
        ("unregistered", "Unregistered catalog"),
        ("missing_catalog", "Missing catalog"),
    ],
)
def test_broken_catalogs_fail_the_gate(catalogs, defect, diagnostic):
    repo, root = catalogs
    assert run(repo, "--extract").returncode == 0
    file = root / "fr.json"
    data = json.loads(file.read_text())
    if defect == "placeholder":
        data["Open {name}"] = "Ouvrir {nom}"
    elif defect == "plural":
        del data["{count} session"]["many"]
    elif defect == "extra_form":
        data["{count} session"]["few"] = "{count} sessions"
    elif defect == "missing":
        del data["Settings"]
    elif defect == "unused":
        data["Stale label"] = "Ancien"
    elif defect == "blank":
        data["Settings"] = " "
    file.write_text(json.dumps(data))
    if defect == "duplicate":
        file.write_text(file.read_text()[:-1] + ', "Settings": "Duplicate"}')
    elif defect == "unregistered":
        (root / "unused.json").write_text("{}")
    elif defect == "missing_catalog":
        file.unlink()
    result = run(repo)
    assert result.returncode != 0
    assert diagnostic in result.stderr


def test_invalid_source_plural_does_not_overwrite_the_last_catalog(catalogs):
    repo, root = catalogs
    assert run(repo, "--extract").returncode == 0
    before = (root / "en.json").read_bytes()
    with (root.parent / "i18n.js").open("a") as stream:
        stream.write('n("One {name}", "Many {wrong}", 2);\n')
    result = run(repo, "--extract")
    assert result.returncode != 0 and "matching named placeholders" in result.stderr
    assert (root / "en.json").read_bytes() == before
