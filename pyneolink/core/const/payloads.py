from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape as _xml_escape


@dataclass(frozen=True)
class XmlTemplate:
    template: str
    document: bool = True
    binary: bool = True

    def format(self, *args, **kwargs):
        text = self.template.format(
            *(_XmlValue(arg) for arg in args),
            **{key: _XmlValue(value) for key, value in kwargs.items()},
        )
        return self._finish(text)

    def __mod__(self, values):
        if isinstance(values, tuple):
            values = tuple(_escape(value) for value in values)
        elif isinstance(values, dict):
            values = {key: _escape(value) for key, value in values.items()}
        else:
            values = _escape(values)
        return self._finish(self.template % values)

    def _finish(self, text: str):
        if self.document:
            text = xml_document.format(inner=Raw(text))
        return text.encode("utf-8") if self.binary else text


@dataclass(frozen=True)
class Raw:
    value: object


class _XmlValue:
    def __init__(self, value: object) -> None:
        self.value = value.value if isinstance(value, Raw) else value
        self.raw = isinstance(value, Raw)

    def __format__(self, spec: str) -> str:
        text = format(self.value, spec)
        return text if self.raw else _escape(text)

    def __getattr__(self, name: str):
        return _XmlValue(getattr(self.value, name))

    def __str__(self) -> str:
        return self.__format__("")


def _escape(value: object) -> str:
    return _xml_escape(str(value), {'"': "&quot;", "'": "&apos;"})


xml_document = XmlTemplate(
    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n{inner}\n</body>',
    document=False,
    binary=False,
)

extension = XmlTemplate(
    '<?xml version="1.0" encoding="UTF-8" ?><Extension version="1.1"><channelId>{channel_id}</channelId></Extension>',
    document=False,
)

extension_binary = XmlTemplate(
    '<?xml version="1.0" encoding="UTF-8" ?><Extension version="1.1"><channelId>{channel_id}</channelId><binaryData>1</binaryData><encryptLen>1024</encryptLen></Extension>',
    document=False,
)

extension_binary_data = XmlTemplate(
    '<?xml version="1.0" encoding="UTF-8" ?><Extension version="1.1"><channelId>{channel_id}</channelId><binaryData>1</binaryData></Extension>',
    document=False,
)

login = XmlTemplate(
    '<LoginUser version="1.1">'
    "<userName>{username}</userName>"
    "<password>{password}</password>"
    "<userVer>1</userVer>"
    "</LoginUser>"
    '<LoginNet version="1.1">'
    "<type>LAN</type>"
    "<udpPort>0</udpPort>"
    "</LoginNet>"
)

led_state = XmlTemplate(
    '<LedState version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<state>{state}</state>"
    "</LedState>"
)

preview_start = XmlTemplate(
    '<Preview version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<handle>{handle}</handle>"
    "<streamType>{stream_type}</streamType>"
    "</Preview>"
)

preview_stop = XmlTemplate(
    '<Preview version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<handle>{handle}</handle>"
    "</Preview>"
)

snapshot = XmlTemplate(
    '<Snap version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<logicChannel>{channel_id}</logicChannel>"
    "<time>0</time>"
    "<fullFrame>0</fullFrame>"
    "<streamType>{stream_type}</streamType>"
    "</Snap>"
)

talk_config = XmlTemplate(
    '<TalkConfig version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<duplex>{duplex}</duplex>"
    "<audioStreamMode>{audio_stream_mode}</audioStreamMode>"
    "<audioConfig>"
    "<audioType>{audio_type}</audioType>"
    "<sampleRate>{sample_rate}</sampleRate>"
    "<samplePrecision>{sample_precision}</samplePrecision>"
    "<lengthPerEncoder>{length_per_encoder}</lengthPerEncoder>"
    "<soundTrack>{sound_track}</soundTrack>"
    "</audioConfig>"
    "</TalkConfig>"
)

audio_play_info = XmlTemplate(
    '<audioPlayInfo version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<playMode>{play_mode}</playMode>"
    "<playDuration>{play_duration}</playDuration>"
    "<playTimes>{play_times}</playTimes>"
    "<onOff>{on_off}</onOff>"
    "</audioPlayInfo>"
)

time_node = XmlTemplate(
    "<{tag}>"
    "<year>{value.year}</year><month>{value.month}</month><day>{value.day}</day>"
    "<hour>{value.hour}</hour><minute>{value.minute}</minute><second>{value.second}</second>"
    "</{tag}>",
    document=False,
    binary=False,
)

flat_time = XmlTemplate(
    "<{prefix}Year>{value.year}</{prefix}Year>"
    "<{prefix}Month>{value.month}</{prefix}Month>"
    "<{prefix}Day>{value.day}</{prefix}Day>"
    "<{prefix}Hour>{value.hour}</{prefix}Hour>"
    "<{prefix}Min>{value.minute}</{prefix}Min>"
    "<{prefix}Sec>{value.second}</{prefix}Sec>",
    document=False,
    binary=False,
)

replay_seek = XmlTemplate(
    '<ReplaySeek version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<seq>{seq}</seq>"
    "{seek_time}"
    "</ReplaySeek>"
)

replay_file_detail = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<channelId>{channel_id}</channelId>"
    "<name>{name}</name>"
    "<supportSub>1</supportSub>"
    "<playSpeed>1</playSpeed>"
    "<streamType>{stream_type}</streamType>"
    "</FileInfo>"
    "</FileInfoList>"
)

replay_stop = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<channelId>{channel_id}</channelId>"
    "<name>{name}</name>"
    "</FileInfo>"
    "</FileInfoList>"
)

hdd_init = XmlTemplate(
    '<HddInitList version="1.1">'
    "<HddInit>"
    "<id>{disk_id}</id>"
    "</HddInit>"
    "</HddInitList>"
)

file_info_compact_type = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<type>{type_value}</type>"
    "<beginTime>{start:%Y%m%d%H%M%S}</beginTime>"
    "<endTime>{end:%Y%m%d%H%M%S}</endTime>"
    "</FileInfoList>"
)

file_info_compact_stream = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<streamType>{stream_type}</streamType>"
    "<beginTime>{start:%Y%m%d%H%M%S}</beginTime>"
    "<endTime>{end:%Y%m%d%H%M%S}</endTime>"
    "</FileInfoList>"
)

file_info_compact_stream_type = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<streamType>{stream_type}</streamType>"
    "<type>{type_value}</type>"
    "<beginTime>{start:%Y%m%d%H%M%S}</beginTime>"
    "<endTime>{end:%Y%m%d%H%M%S}</endTime>"
    "</FileInfoList>"
)

file_info_nested = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<streamType>{stream_type}</streamType>"
    "{start_time}"
    "{end_time}"
    "</FileInfoList>"
)

file_info_nested_type = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<streamType>{stream_type}</streamType>"
    "<type>{file_type}</type>"
    "{start_time}"
    "{end_time}"
    "</FileInfoList>"
)

file_info_flat = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<streamType>{stream_type}</streamType>"
    "{begin_time}"
    "{end_time}"
    "</FileInfoList>"
)

day_records_range = XmlTemplate(
    '<DayRecords version="1.1">'
    "{start_time}"
    "{end_time}"
    "<DayRecordList><DayRecord>"
    "<index>0</index>"
    "<channelId>{channel_id}</channelId>"
    "</DayRecord></DayRecordList>"
    "</DayRecords>"
)

file_handle_request = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<channelId>{channel_id}</channelId>"
    "<streamType>{stream_type}</streamType>"
    "<recordType>{record_types}</recordType>"
    "{start_time}"
    "{end_time}"
    "</FileInfo>"
    "</FileInfoList>"
)

files_for_handle = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<channelId>{channel_id}</channelId>"
    "<handle>{handle}</handle>"
    "</FileInfo>"
    "</FileInfoList>"
)

day_record_nested = XmlTemplate(
    '<DayRecords version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<year>{target.year}</year><month>{target.month}</month><day>{target.day}</day>"
    "</DayRecords>"
)

day_record_compact = XmlTemplate(
    '<DayRecords version="1.1">'
    "<channelId>{channel_id}</channelId>"
    "<date>{target:%Y%m%d}</date>"
    "</DayRecords>"
)

replay_download = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<channelId>{channel_id}</channelId>"
    "<supportSub>1</supportSub>"
    "<streamType>{stream_type}</streamType>"
    "{start_time}"
    "<playSpeed>1</playSpeed>"
    "</FileInfo>"
    "</FileInfoList>"
)

playback_download = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<logicChnBitmap>255</logicChnBitmap>"
    "<channelId>{channel_id}</channelId>"
    "<supportSub>{support_sub}</supportSub>"
    "<streamType>{stream_type}</streamType>"
    "{start_time}"
    "{end_time}"
    "</FileInfo>"
    "</FileInfoList>"
)

playback_download_no_support = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<logicChnBitmap>255</logicChnBitmap>"
    "<channelId>{channel_id}</channelId>"
    "<streamType>{stream_type}</streamType>"
    "{start_time}"
    "{end_time}"
    "</FileInfo>"
    "</FileInfoList>"
)

download_file = XmlTemplate(
    '<FileInfoList version="1.1">'
    "<FileInfo>"
    "<channelId>{channel_id}</channelId>"
    "{fields}"
    "</FileInfo>"
    "</FileInfoList>"
)

download_id_field = XmlTemplate("<Id>{file_id}</Id>", document=False, binary=False)
download_file_name_field = XmlTemplate("<fileName>{file_id}</fileName>", document=False, binary=False)
download_name_as_file_name_field = XmlTemplate("<fileName>{name}</fileName>", document=False, binary=False)
download_name_field = XmlTemplate("<name>{name}</name>", document=False, binary=False)
download_handle_field = XmlTemplate("<handle>{handle}</handle>", document=False, binary=False)
download_stream_type_field = XmlTemplate("<streamType>{stream_type}</streamType>", document=False, binary=False)
download_file_type_field = XmlTemplate("<fileType>{file_type}</fileType>", document=False, binary=False)
download_record_type_field = XmlTemplate("<recordType>{record_type}</recordType>", document=False, binary=False)
