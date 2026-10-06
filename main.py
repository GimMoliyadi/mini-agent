"""Legacy CLI entry and import alias for the packaged runtime."""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_agent.runtime import *

from mini_agent import runtime as _runtime

if __name__ == "__main__":
    raise SystemExit(_runtime.main())
else:
    sys.modules[__name__] = _runtime
