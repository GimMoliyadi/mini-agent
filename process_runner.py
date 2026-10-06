"""Compatibility import for the packaged bounded subprocess runner."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.process_runner import *

from mini_agent import process_runner as _implementation
sys.modules[__name__] = _implementation
