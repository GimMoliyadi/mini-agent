"""Compatibility import; implementation lives in mini_agent.runtime_guards."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.runtime_guards import *

from mini_agent import runtime_guards as _implementation
sys.modules[__name__] = _implementation
