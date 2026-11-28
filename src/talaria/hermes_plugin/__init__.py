"""Optional Hermes plugin. Runs inside Hermes, independently of Talaria's server."""

# Fingerprint the files before the rest loads, so one replaced meanwhile reads as changed.
from . import release
from .bridge import register

__all__ = ["register"]
__version__ = release.PLUGIN_VERSION
