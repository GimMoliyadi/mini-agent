"""Compatibility import; implementation lives in mini_agent.sandbox."""
import sys
from mini_agent import sandbox as _implementation
sys.modules[__name__] = _implementation
