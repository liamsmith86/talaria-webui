"""Optional observations must not add writes or turn metadata drift into failures."""

import os
import sqlite3
import sys
import types

import pytest

from talaria.hermes_plugin.observations import Observations, number, reasoning


def test_reading_observations_does_not_create_or_touch_the_store(tmp_path):
    observations = Observations(tmp_path)
    assert observations.read("missing") is None
    assert not (tmp_path / "talaria").exists()
    observations.save("session", 1, {"model": "known", "message_id": 99})
    path = tmp_path / "talaria/observations.db"
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    assert observations.read("session", 1) == {"model": "known", "message_id": 1}
    assert path.stat().st_mtime_ns == 1_000_000_000


def test_details_follow_the_durable_id_when_compaction_renumbers_a_reply(tmp_path):
    observations = Observations(tmp_path)
    observations.save("session", 1, {"model": "kept"}, uid="uid-1")
    assert observations.read("session", 2) is None
    assert observations.read("session", 2, "other") is None
    assert observations.read("session", 2, "uid-1") == {
        "model": "kept",
        "message_id": 1,
        "uid": "uid-1",
    }


def test_store_from_an_older_plugin_still_reads_and_then_migrates(tmp_path):
    directory = tmp_path / "talaria"
    directory.mkdir()
    with sqlite3.connect(directory / "observations.db") as db:
        db.execute(
            "CREATE TABLE responses (session TEXT, message INTEGER, "
            "data TEXT NOT NULL, PRIMARY KEY(session, message))"
        )
        db.execute("INSERT INTO responses VALUES ('session', 1, '{\"model\": \"old\"}')")
    observations = Observations(tmp_path)
    assert observations.read("session", 1)["model"] == "old"
    assert observations.read("session", 2, "uid") is None
    assert observations.read("session")["model"] == "old"
    assert observations.read_compaction("session") is None
    observations.save("session", 2, {"model": "new"}, uid="uid")
    assert observations.read("session", 3, "uid")["model"] == "new"
    assert observations.read("session", 1)["model"] == "old"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    module = types.ModuleType("hermes_constants")
    module.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", module)
    return tmp_path


def test_compaction_size_survives_a_rotation_and_a_rewind_forgets_it(hermes_home):
    observations = Observations(hermes_home)
    observations.save("old", 1, {"model": "known", "context_max": 1000})
    observations.compacted(session_id="old", after_tokens=None)
    assert observations.read_compaction("old") is None
    observations.compacted(session_id="new", old_session_id="old", after_tokens=120)
    saved = observations.read_compaction("new")
    assert (saved["tokens"], saved["maximum"], saved["model"]) == (120, 1000, "known")
    observations.forget_compaction("new")
    assert observations.read_compaction("new") is None


def test_deleting_a_session_removes_its_details_but_archiving_keeps_them(hermes_home):
    observations = Observations(hermes_home)
    observations.deleted(session_id="missing", reason="delete")
    assert not (hermes_home / "talaria").exists(), "A delete must not create the optional store"
    for session in ("gone", "lineage", "kept"):
        observations.save(session, 1, {"model": session})
        observations.compacted(session_id=session, after_tokens=5)
    observations.deleted(session_id="gone", reason="archive")
    assert observations.read("gone", 1)
    observations.deleted(session_id="gone", deleted_ids=["gone", "lineage"], reason="delete")
    for session in ("gone", "lineage"):
        assert observations.read(session, 1) is None
        assert observations.read_compaction(session) is None
    assert observations.read("kept", 1) and observations.read_compaction("kept")


@pytest.mark.parametrize("broken", ["not-json", "null", "[]", '"unknown format"'])
def test_corrupt_observation_does_not_hide_other_responses(tmp_path, broken):
    observations = Observations(tmp_path)
    observations.save("session", 1, {"model": "first"})
    observations.save("session", 2, {"model": "second"})
    with sqlite3.connect(tmp_path / "talaria/observations.db") as db:
        db.execute("UPDATE responses SET data = ? WHERE message = 2", (broken,))
    assert observations.read("session", 2) is None
    assert observations.read("session", 1)["model"] == "first"


def test_corrupt_or_unrecognized_store_is_optional(tmp_path):
    directory = tmp_path / "talaria"
    directory.mkdir()
    path = directory / "observations.db"
    path.write_text("not a database")
    assert Observations(tmp_path).read("session") is None
    path.unlink()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future_observations (message INTEGER)")
    assert Observations(tmp_path).read("session") is None


def test_failed_observation_initialization_closes_its_connection(tmp_path, monkeypatch):
    class FailedDatabase:
        closed = False

        def execute(self, *args):
            raise sqlite3.DatabaseError("Cannot initialize")

        def close(self):
            self.closed = True

    db = FailedDatabase()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: db)
    with pytest.raises(sqlite3.DatabaseError):
        Observations(tmp_path).connect()
    assert db.closed


def test_unrecognized_provider_observations_stay_unknown():
    assert number(10**1000) is None
    assert number(float("inf")) is None
    assert number(True) is None
    assert reasoning({"thinking": {"type": ["unexpected"]}}) is None
    assert reasoning({"thinking": {"type": {"unexpected": True}}}) is None
    assert reasoning({"thinking": {"type": "adaptive"}}) == "enabled"


def test_new_model_call_cannot_inherit_prior_usage_or_accept_a_late_hook(tmp_path):
    observations = Observations(tmp_path)
    observations.before(
        task_id="s",
        turn_id="t",
        api_request_id="first",
        request={},
        started_at=1,
        model="first-model",
        provider="test",
    )
    observations.after(
        task_id="s",
        turn_id="t",
        api_request_id="first",
        usage={"prompt_tokens": 100, "output_tokens": 20},
        api_duration=2,
    )
    observations.before(
        task_id="s",
        turn_id="t",
        api_request_id="second",
        request={"reasoning_effort": "high"},
        started_at=4,
        model="second-model",
        provider="test",
    )
    observations.after(
        task_id="s",
        turn_id="t",
        api_request_id="first",
        usage={"prompt_tokens": 999},
        api_duration=99,
    )
    pending = observations.pending[("s", "t")]
    assert pending["model"] == "second-model"
    assert pending["reasoning"] == "high"
    assert pending["started_at"] == 1
    assert "usage" not in pending and "duration_seconds" not in pending


def test_search_reads_an_older_hermes_display_once_per_session():
    from talaria.hermes_plugin.search import conversation_ids

    reads = []

    class OlderHermes:
        def get_messages(self, sid, include_compacted=False):
            reads.append(sid)
            return [{"id": 1}]

    shown = {}
    for _ in range(3):
        assert conversation_ids(OlderHermes(), "session", shown) == {1}
    assert reads == ["session"]
