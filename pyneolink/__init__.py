"""Python API for Reolink/Neolink cameras."""

from .camera import Camera
from .battery import Battery, BatteryInfo, BatteryInfoUpdates, parse_battery_xml
from .config import CameraConfig, Config, config_from_dict, load_config
from .core.const import EVENTS
from .motion import CameraEvent, CameraEvents, Motion, parse_motion_events
from .recorder import StreamRecorder
from .sd_card import DangerousSdCardOperation, DownloadSizeMismatch, SdCard, SdCardFile
from .stream_server import StreamServer, serve_streams
from .voice import TalkConfig, Voice

__all__ = [
    "Camera",
    "CameraConfig",
    "CameraEvent",
    "CameraEvents",
    "Motion",
    "Battery",
    "BatteryInfo",
    "BatteryInfoUpdates",
    "Config",
    "DangerousSdCardOperation",
    "DownloadSizeMismatch",
    "SdCard",
    "SdCardFile",
    "StreamServer",
    "StreamRecorder",
    "EVENTS",
    "TalkConfig",
    "Voice",
    "config_from_dict",
    "load_config",
    "parse_motion_events",
    "parse_battery_xml",
    "serve_streams",
    "__version__",
]
__version__ = "0.1.0"
