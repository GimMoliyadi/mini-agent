"""Compatibility import for the packaged bounded subprocess runner."""
import sys
from mini_agent import process_runner as _implementation
sys.modules[__name__] = _implementation
