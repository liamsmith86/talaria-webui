"""Dynamic entry points; keep references explicit so removed names can be reviewed."""

from types import SimpleNamespace

from talaria.cli import development_app

# Uvicorn imports this factory by the string "talaria.cli:development_app".
development_app  # noqa: B018 -- Vulture whitelist reference, not executable code

# Written by the API compression adapter; consumed by Hermes's native agent/lease code.
hermes_agent = SimpleNamespace()
hermes_agent._session_db_created  # noqa: B018
hermes_agent._end_session_on_close  # noqa: B018
hermes_agent._active_session_turn_lease_holder  # noqa: B018
hermes_agent._active_session_turn_lease_ttl_seconds  # noqa: B018
