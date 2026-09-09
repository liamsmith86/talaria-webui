"""Optional Hermes plugin. Runs inside Hermes, independently of Talaria's server."""

from .bridge import register

__all__ = ["register"]
