"""Compatibility import; implementation lives in mini_agent.verifier."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.verifier import *

from mini_agent import verifier as _implementation
sys.modules[__name__] = _implementation
