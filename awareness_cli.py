"""Compatibility import; implementation lives in mini_agent.awareness_cli."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.awareness_cli import *

from mini_agent import awareness_cli as _implementation
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
