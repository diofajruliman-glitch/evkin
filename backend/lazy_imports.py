"""Small, thread-safe lazy imports for optional request-time dependencies."""
from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any


class LazyModule:
    """Load a module on first attribute access, not while routes are registered."""

    def __init__(self, module_name: str):
        self._module_name = module_name
        self._module: ModuleType | None = None
        self._lock = threading.Lock()

    def _load(self) -> ModuleType:
        module = self._module
        if module is None:
            with self._lock:
                module = self._module
                if module is None:
                    module = importlib.import_module(self._module_name)
                    self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


def lazy_module(module_name: str) -> LazyModule:
    return LazyModule(module_name)
