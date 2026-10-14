"""Compatibility import; implementation lives in mini_agent.launcher."""
import sys
from mini_agent import launcher as _implementation
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
