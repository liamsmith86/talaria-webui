"""Deliberately broken inputs must be rejected by the real static tools."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from contrib import quality

pytestmark = pytest.mark.quality

ROOT = Path(__file__).resolve().parents[1]


def prepare_gate_repo(destination):
    for name in (
        "pyproject.toml",
        "uv.lock",
        "package.json",
        "package-lock.json",
        ".npmrc",
        "eslint.config.js",
        "contrib/quality.py",
        "contrib/check.py",
        ".gitignore",
    ):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (destination / "src/talaria").mkdir(parents=True)
    (destination / ".github/scripts").mkdir(parents=True)
    (destination / ".github/scripts/check.py").write_text("print(42)\n")
    (destination / ".git/info/exclude").write_text("node_modules\n")
    # Avoid downloads in gate tests; the staged snapshot still verifies the lock identity.
    (destination / "node_modules").symlink_to(ROOT / "node_modules")


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


@pytest.mark.parametrize(
    "code,rule",
    [
        ("def f():\n    breakpoint()\n", "T100"),
        (
            "def f(x):\n" + "".join(f"    if x == {n}:\n        print(x)\n" for n in range(10)),
            "C901",
        ),
        (
            "def f(x):\n" + "".join(f"    if x == {n}:\n        print(x)\n" for n in range(13)),
            "PLR0912",
        ),
        ("def f():\n" + "    print(1)\n" * 51, "PLR0915"),
    ],
)
def test_production_python_gates(code, rule):
    result = subprocess.run(
        ["ruff", "check", "--stdin-filename", "src/talaria/example.py", "-"],
        input=code,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert rule in result.stdout


def test_vulture_rejects_function_after_last_caller_is_removed(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def orphan():\n    return 42\n\nprint(orphan())\n")
    command = ["vulture", str(source), "--min-confidence", "60"]
    assert subprocess.run(command, capture_output=True).returncode == 0
    source.write_text("def orphan():\n    return 42\n")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "unused function 'orphan'" in result.stdout


@pytest.mark.parametrize(
    "code,rule",
    [
        ("missingName();", "no-undef"),
        ("const unused = 42;", "no-unused-vars"),
        ("debugger;", "no-debugger"),
        ("function Widget({enabled}) { if(enabled) useState(0); }", "react-hooks/rules-of-hooks"),
        (
            "function Widget({value}) { useEffect(() => alert(value), []); }",
            "react-hooks/exhaustive-deps",
        ),
    ],
)
def test_javascript_gates(code, rule):
    result = subprocess.run(
        [
            "node",
            "node_modules/eslint/bin/eslint.js",
            "--stdin",
            "--stdin-filename",
            "src/talaria/static/example.js",
            "--max-warnings",
            "0",
        ],
        input=code,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert rule in result.stdout


def test_vendored_javascript_is_excluded():
    result = subprocess.run(
        [
            "node",
            "node_modules/eslint/bin/eslint.js",
            "--stdin",
            "--stdin-filename",
            "src/talaria/static/vendor/example.js",
            "--no-warn-ignored",
        ],
        input="invalid !!!",
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    assert result.returncode == 0


@pytest.mark.parametrize(
    "command,name,content,diagnostic",
    [
        (["shellcheck"], "bad.sh", "#!/bin/sh\necho $unquoted\n", "SC2086"),
        (
            ["actionlint", "-oneline"],
            "bad.yml",
            "on: push\njobs:\n  bad:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: echo ${{ not_a_context.foo }}\n",
            "not_a_context",
        ),
    ],
)
def test_shell_and_workflow_gates(tmp_path, command, name, content, diagnostic):
    path = tmp_path / name
    path.write_text(content)
    result = subprocess.run([*command, str(path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert diagnostic in result.stdout + result.stderr


def test_javascript_cache_tracks_lock_and_settings(tmp_path):
    for name in ("package.json", "package-lock.json", ".npmrc"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    before = quality.js_identity(tmp_path)
    (tmp_path / ".npmrc").write_text("ignore-scripts=true\nmin-release-age=14\n")
    assert quality.js_identity(tmp_path) != before


def test_hanging_test_is_terminated_with_a_diagnostic(tmp_path):
    source = tmp_path / "test_hang.py"
    source.write_text("import time\ndef test_hang():\n    time.sleep(60)\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--timeout=0.2", "--timeout-method=thread", str(source)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Timeout" in result.stdout + result.stderr
