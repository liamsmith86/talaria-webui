"""The installed command: reachable by name only when that really runs this installation."""

import os

import pytest

from talaria import setup_command as command


@pytest.fixture
def install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.setenv("TALARIA_SHELL_PATH", str(home / ".local/bin"))
    root = tmp_path / "install"
    (root / "bin").mkdir(parents=True)
    (root / "bin/talaria").write_text("#!/bin/sh\n")
    (root / "bin/talaria").chmod(0o755)
    return root


def link(root):
    return root.parent / "home/.local/bin/talaria"


def test_a_linked_command_on_the_path_is_printed_by_name(install):
    assert command.install_command(install) == "talaria"
    assert os.readlink(link(install)) == str(install / "bin/talaria")
    # Rerunning setup finds its own link.
    assert command.install_command(install) == "talaria"


def test_a_directory_off_the_path_gets_the_full_path_and_a_hint(install, monkeypatch, capsys):
    monkeypatch.setenv("TALARIA_SHELL_PATH", "/usr/bin")
    assert command.install_command(install) == str(install / "bin/talaria")
    assert ".local/bin to your PATH" in capsys.readouterr().out


def test_another_talaria_is_left_alone(install):
    link(install).parent.mkdir(parents=True)
    link(install).write_text("someone else's")
    assert command.install_command(install) == str(install / "bin/talaria")
    assert link(install).read_text() == "someone else's"
    assert command.links_to(install) == []


def test_an_earlier_talaria_on_the_path_keeps_the_full_path(install, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "talaria").write_text("#!/bin/sh\n")
    (other / "talaria").chmod(0o755)
    monkeypatch.setenv("TALARIA_SHELL_PATH", f"{other}{os.pathsep}{link(install).parent}")
    assert command.install_command(install) == str(install / "bin/talaria")


@pytest.mark.parametrize(
    "setup,lingering,shown",
    [
        ({"kind": "systemd", "scope": "user"}, False, True),
        ({"kind": "systemd", "scope": "user"}, True, False),
        ({"kind": "systemd", "scope": "system"}, False, False),
        ({"kind": "launchd", "scope": "user"}, False, False),
        ({}, False, False),
    ],
)
def test_linger_hint_appears_only_where_it_applies(
    setup, lingering, shown, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(command, "LINGER_DIRECTORY", tmp_path)
    if lingering:
        (tmp_path / command.pwd.getpwuid(os.geteuid()).pw_name).touch()
    command.print_linger_hint(setup)
    assert ("loginctl enable-linger" in capsys.readouterr().out) is shown
