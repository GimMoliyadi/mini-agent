"""Compatibility import; implementation lives in mini_agent.diagnostics."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.diagnostics import *

from mini_agent import diagnostics as _implementation
sys.modules[__name__] = _implementation
