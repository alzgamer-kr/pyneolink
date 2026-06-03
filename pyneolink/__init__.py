"""Python API for Reolink/Neolink cameras."""

from .camera import Camera
from .battery import Battery, BatteryInfo, BatteryInfoUpdates, parse_battery_xml
from .config import CameraConfig, Config, config_from_dict, load_config
from .sd_card import DangerousSdCardOperation, DownloadSizeMismatch, SdCard, SdCardFile
from .stream_server import StreamServer, serve_streams

__all__ = [
    "Camera",
    "CameraConfig",
    "Battery",
    "BatteryInfo",
    "BatteryInfoUpdates",
    "Config",
    "DangerousSdCardOperation",
    "DownloadSizeMismatch",
    "SdCard",
    "SdCardFile",
    "StreamServer",
    "config_from_dict",
    "load_config",
    "parse_battery_xml",
    "serve_streams",
    "__version__",
]
__version__ = "0.1.0"
