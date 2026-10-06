"""Compatibility import; implementation lives in mini_agent.sandbox."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.sandbox import *

from mini_agent import sandbox as _implementation
sys.modules[__name__] = _implementation
