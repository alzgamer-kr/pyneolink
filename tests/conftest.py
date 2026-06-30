from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def pytest_configure(config) -> None:
    if getattr(config.option, "basetemp", None) is None:
        root = Path.cwd() / ".tmp-pytest"
        root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(root / uuid4().hex)
