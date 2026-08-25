from __future__ import annotations

from pyneolink import Camera


SETTINGS = {
    "uuid": "ABCDEF0123456789",
    "username": "admin",
    "password": "password",
}


def list_presets() -> None:
    with Camera(**SETTINGS) as camera:
        for preset in camera.ptz().presets():
            print(preset)


def goto_preset(preset_id: int, *, confirm: bool = False) -> None:
    if not confirm:
        print("Refusing to move the camera. Pass confirm=True after selecting the exact preset.")
        return
    with Camera(**SETTINGS) as camera:
        # Required by the tested Argus PT Ultra before preset recall.
        camera.snapshot()
        camera.ptz().goto_preset(preset_id)
        print(f"Recalled preset {preset_id}.")


if __name__ == "__main__":
    list_presets()
