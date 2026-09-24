"""Compatibility facade.

Core identity/configuration models live in ``core.models`` and industry models live in
``modules.mod_caficultura.models``. New code should import from those packages directly.
"""
from core.models import *  # noqa: F401,F403
from modules.mod_caficultura.models import *  # noqa: F401,F403
