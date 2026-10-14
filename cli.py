"""Compatibility import; implementation lives in mini_agent.cli."""
import sys
from mini_agent import cli as _implementation
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    from mini_agent.launcher import main
    raise SystemExit(main())
