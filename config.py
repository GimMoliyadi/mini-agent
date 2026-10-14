"""Compatibility import; implementation lives in mini_agent.config."""
import sys
from mini_agent import config as _implementation
sys.modules[__name__] = _implementation
