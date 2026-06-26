from __future__ import annotations

from .ir import Ir
from .pir import Pir


class Settings:
    def __init__(self, camera) -> None:
        self.camera = camera
        self.ir = Ir(camera)
        self.pir = Pir(camera)


__all__ = ["Ir", "Pir", "Settings"]
