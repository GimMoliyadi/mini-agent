"""Compatibility import; implementation lives in mini_agent.recovery."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.recovery import *

from mini_agent import recovery as _implementation
sys.modules[__name__] = _implementation
