"""Compatibility import; implementation lives in mini_agent.awareness."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.awareness import *

from mini_agent import awareness as _implementation
sys.modules[__name__] = _implementation
