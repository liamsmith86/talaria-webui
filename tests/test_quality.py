"""Talaria's own static gates."""

import shutil
from pathlib import Path

import pytest

from contrib import quality

pytestmark = pytest.mark.quality

ROOT = Path(__file__).resolve().parents[1]


def test_every_fixed_wait_says_why_it_cannot_race():
    # Tests wait on conditions. A fixed wait is for a window where nothing may change.
    unexplained = [
        f"{path.relative_to(ROOT)}:{number}"
        for folder in ("tests", "e2e/scripted")
        for path in (ROOT / folder).rglob("test_*.py")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "wait_for_timeout(" in line and "# Fixed wait: " not in line
    ]
    assert not unexplained


@pytest.mark.parametrize(
    "name,content",
    [
        ("broken.json", '{"a": }'),
        ("broken.toml", "broken = ["),
        ("broken.yaml", "a: 1\na: 2\n"),
        ("conflict.txt", "<" * 7 + " HEAD\nconflict\n" + "=" * 7 + "\n"),
        ("key.txt", "-" * 5 + "BEGIN " + "PRIVATE" + " KEY" + "-" * 5 + "\n"),
        ("large.bin", "x" * (1024 * 1024 + 1)),
    ],
    ids=["json", "toml", "yaml", "conflict", "private-key", "large-file"],
)
def test_hygiene_rejects_invalid_files(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    with pytest.raises(SystemExit):
        quality.hygiene([path])


def test_hygiene_accepts_workflow_on_key_and_unicode(tmp_path):
    path = tmp_path / "workflow.yml"
    path.write_text('on: [pull_request]\nname: "Talaria — CI"\n')
    quality.hygiene([path])


def test_javascript_cache_tracks_lock_and_settings(tmp_path):
    for name in ("package.json", "package-lock.json", ".npmrc"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    before = quality.js_identity(tmp_path)
    (tmp_path / ".npmrc").write_text("ignore-scripts=true\nmin-release-age=14\n")
    assert quality.js_identity(tmp_path) != before
