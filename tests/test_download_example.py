import csv

from examples.download_test import DownloadMessageTrace
from pyneolink.core.bc import Header, Message
from pyneolink.core.const import MSG, MSG_CLASS


def test_download_message_trace_records_tx_and_rx_without_consuming_messages(tmp_path):
    reply = Message(
        Header(MSG.FILE_PLAYBACK, 4, 0, 0, 7, 300, MSG_CLASS.MODERN),
        payload=b"done",
        raw_payload_len=4,
    )

    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.sent = []

        def _recv_direct(self, timeout=None, *, binary_playback_331=False):
            return reply

        def _send_modern(self, msg_id, msg_num, payload=b"", **kwargs):
            self.sent.append((msg_id, msg_num, payload, kwargs))

    camera = FakeCamera()
    trace_path = tmp_path / "messages.csv"

    with DownloadMessageTrace(camera, trace_path) as trace:
        trace.select_file(1, 5, "recording.mp4")
        camera._send_modern(MSG.FILE_PLAYBACK, 7, b"<FileInfo/>")
        received = camera._recv_direct(timeout=1.0, binary_playback_331=True)

    assert received is reply
    assert camera.sent[0][:3] == (MSG.FILE_PLAYBACK, 7, b"<FileInfo/>")
    with trace_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["direction"], row["msg_id"], row["response_code"]) for row in rows] == [
        ("tx", "143", ""),
        ("rx", "143", "300"),
    ]
    assert all(row["file_index"] == "1/5" for row in rows)
    assert all(row["file_name"] == "recording.mp4" for row in rows)
