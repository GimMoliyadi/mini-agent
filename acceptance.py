"""Compatibility import; implementation lives in mini_agent.verifier."""
import sys
from mini_agent import verifier as _implementation
sys.modules[__name__] = _implementation
