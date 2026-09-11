"""Dynamic entry points; keep references explicit so removed names can be reviewed."""

from talaria.cli import development_app

# Uvicorn imports this factory by the string "talaria.cli:development_app".
development_app  # noqa: B018 -- Vulture whitelist reference, not executable code
