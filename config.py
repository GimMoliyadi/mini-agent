"""Compatibility import; implementation lives in mini_agent.config."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.config import *

from mini_agent import config as _implementation
sys.modules[__name__] = _implementation
