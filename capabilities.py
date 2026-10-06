"""Compatibility import; implementation lives in mini_agent.capabilities."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.capabilities import *

from mini_agent import capabilities as _implementation
sys.modules[__name__] = _implementation
