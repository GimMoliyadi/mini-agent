"""Local event metadata: no tool arguments, file bodies or model text in traces."""

import json
from pathlib import Path
from threading import Lock
from time import time
from collections.abc import Callable


class Events:
    def __init__(self, path: Path | None = None, callback: Callable[[dict], None] | None = None):
        self.path = path
        self.callback = callback
        self.events: list[dict] = []
        self.lock = Lock()

    def emit(self, event: str, **metadata) -> None:
        item = {"event": event, "time": time(), **metadata}
        with self.lock:
            self.events.append(item)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            if self.callback:
                self.callback(item)
