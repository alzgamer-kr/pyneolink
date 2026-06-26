from __future__ import annotations

from enum import IntEnum, StrEnum


class MAGIC(IntEnum):
    BAICHUAN = 0x0ABCDEF0
    BAICHUAN_REVERSED = 0x0FEDCBA0
    DISCOVERY = 0x2A87CF3A
    UDP_ACK = 0x2A87CF20
    UDP_DATA = 0x2A87CF10


class MSG_CLASS(IntEnum):
    LEGACY = 0x6514
    MODERN_REPLY = 0x6614
    MODERN = 0x6414
    FILE_DOWNLOAD = 0x6482
    MODERN_ZERO = 0x0000


class BCMEDIA(IntEnum):
    AUDIO_ADPCM_MAGIC = 0x62773130
    AUDIO_ADPCM_STREAM_TYPE = 0x0100


class AUDIO_PLAY(IntEnum):
    SIREN_MODE = 0
    SIREN_TRIGGER = 0
    DEFAULT_TIMES = 1


class MSG(IntEnum):
    LOGIN = 1
    LOGOUT = 2
    VIDEO = 3
    VIDEO_STOP = 4
    FILE_REPLAY = 5
    FILE_REPLAY_STOP = 7
    TALKABILITY = 10
    TALKRESET = 11
    FILE_DOWNLOAD_VIDEO = 8
    FILE_DOWNLOAD = 13
    FILE_INFO_LIST = 14
    FILE_INFO_LIST_ALT = 15
    FILE_INFO_LIST_ALT2 = 16
    REBOOT = 23
    MOTION_REQUEST = 31
    MOTION = 33
    VERSION = 80
    HDD_INFO = 102
    HDD_INIT = 103
    SNAP = 109
    UID = 114
    REPLAY_SEEK = 123
    DAY_RECORDS = 142
    FILE_PLAYBACK = 143
    FILE_PLAYBACK_STOP = 144
    GET_LED = 208
    SET_LED = 209
    GET_PIR_ALARM = 212
    SET_PIR_ALARM = 213
    TALKCONFIG = 201
    TALK = 202
    UDP_KEEPALIVE = 234
    BATTERY = 253
    PLAY_AUDIO = 263


class EVENTS(StrEnum):
    human = "human"
    vehicle = "vehicle"
    motion = "motion"
    none = "none"
    unknown = "unknown"
