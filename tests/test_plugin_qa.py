"""Optional observations must not add writes or turn metadata drift into failures."""

import os
import sqlite3

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
