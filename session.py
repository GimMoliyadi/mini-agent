"""Compatibility import; implementation lives in mini_agent.session."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.session import *

from mini_agent import session as _implementation
sys.modules[__name__] = _implementation
