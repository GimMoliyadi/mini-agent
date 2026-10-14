"""Compatibility import; implementation lives in mini_agent.runtime_guards."""
import sys
from mini_agent import runtime_guards as _implementation
sys.modules[__name__] = _implementation
