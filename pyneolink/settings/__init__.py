from __future__ import annotations

from .ir import Ir
from .pir import Pir


class Settings:
    """Facade for camera settings helpers such as PIR and IR."""

    def __init__(self, camera) -> None:
        """Create a settings facade.

        :param camera: Connected or connectable `Camera` instance.
        """
        self.camera = camera
        self.ir = Ir(camera)
        self.pir = Pir(camera)


__all__ = ["Ir", "Pir", "Settings"]
