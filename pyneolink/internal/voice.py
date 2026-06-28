from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator

from pyneolink.core.bc import ProtocolError, find_text
from pyneolink.core.const import BCMEDIA, msg, payloads


_IMA_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
_IMA_STEP_TABLE = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767)


@dataclass(frozen=True)
class TalkConfig:
    """Camera two-way talk audio configuration.

    :param channel_id: Camera channel id.
    :param duplex: Duplex mode reported by the camera.
    :param audio_stream_mode: Audio stream mode reported by the camera.
    :param audio_type: Audio codec type, currently expected to be `adpcm`.
    :param sample_rate: Source sample rate in Hz.
    :param sample_precision: Source sample precision in bits.
    :param length_per_encoder: Camera encoder block size.
    :param sound_track: Audio channel layout, usually `mono`.
    """

    channel_id: int
    duplex: str
    audio_stream_mode: str
    audio_type: str
    sample_rate: int
    sample_precision: int
    length_per_encoder: int
    sound_track: str

    @property
    def block_align(self) -> int:
        return self.length_per_encoder // 2 + 4

    @property
    def samples_per_block(self) -> int:
        return (self.block_align - 4) * 2 + 1


@dataclass(frozen=True)
class AudioFileInfo:
    """Audio file metadata returned by FFprobe validation.

    :param path: Audio file path.
    :param format_name: Container/format name.
    :param codec_name: Audio codec name.
    :param sample_rate: Sample rate in Hz when known.
    :param channels: Number of channels when known.
    :param duration: Duration in seconds when known.
    """

    path: Path
    format_name: str
    codec_name: str
    sample_rate: int | None
    channels: int | None
    duration: float | None


@dataclass
class _PcmStats:
    blocks: int = 0
    samples: int = 0
    peak: int = 0

    def observe(self, data: bytes) -> None:
        samples = struct.unpack("<" + "h" * (len(data) // 2), data[: len(data) - len(data) % 2])
        self.blocks += 1
        self.samples += len(samples)
        if samples:
            self.peak = max(self.peak, max(abs(sample) for sample in samples))


def parse_talk_config(root, *, channel_id: int) -> TalkConfig:  # noqa: ANN001
    if root is None:
        raise ProtocolError(msg.Error.VoiceNotSupported)
    audio_config = root.find(".//audioConfigList/audioConfig")
    if audio_config is None:
        audio_config = root.find(".//audioConfig")
    duplex = find_text(root, "duplex") or "FDX"
    audio_stream_mode = find_text(root, "audioStreamMode") or "followVideoStream"
    if audio_config is None:
        raise ProtocolError(msg.Error.VoiceNotSupported)
    audio_type = find_text(audio_config, "audioType") or ""
    sample_rate = _required_int(audio_config, "sampleRate")
    sample_precision = _required_int(audio_config, "samplePrecision")
    length_per_encoder = _required_int(audio_config, "lengthPerEncoder")
    sound_track = find_text(audio_config, "soundTrack") or "mono"
    config = TalkConfig(
        channel_id=channel_id,
        duplex=duplex,
        audio_stream_mode=audio_stream_mode,
        audio_type=audio_type,
        sample_rate=sample_rate,
        sample_precision=sample_precision,
        length_per_encoder=length_per_encoder,
        sound_track=sound_track,
    )
    if config.block_align <= 4 or config.sample_rate <= 0:
        raise ProtocolError(msg.Error.VoiceNotSupported)
    return config


def serialize_bcmedia_adpcm(block: bytes) -> bytes:
    block_payload_size = len(block) + 4
    header = struct.pack(
        "<IHHHH",
        BCMEDIA.AUDIO_ADPCM_MAGIC,
        block_payload_size,
        block_payload_size,
        BCMEDIA.AUDIO_ADPCM_STREAM_TYPE,
        (len(block) - 4) // 2,
    )
    padding = b"\x00" * ((8 - len(block) % 8) % 8)
    return header + block + padding


def talk_config_payload(config: TalkConfig) -> bytes:
    return payloads.talk_config.format(
        channel_id=config.channel_id,
        duplex=config.duplex,
        audio_stream_mode=config.audio_stream_mode,
        audio_type=config.audio_type,
        sample_rate=config.sample_rate,
        sample_precision=config.sample_precision,
        length_per_encoder=config.length_per_encoder,
        sound_track=config.sound_track,
    )


def adpcm_blocks_from_file(
    file: str | Path,
    config: TalkConfig,
    *,
    volume: float = 1.0,
    codec: str = "python",
    on_debug: Callable[[str], None] | None = None,
) -> Iterator[bytes]:
    source = validate_audio_file(file, on_debug=on_debug)
    if on_debug is not None:
        on_debug(
            "file conversion "
            f"input={source.codec_name}/{source.sample_rate or '?'}Hz/{source.channels or '?'}ch "
            f"output=pcm_s16le/{config.sample_rate}Hz/mono -> adpcm"
        )
    if codec == "ffmpeg":
        yield from adpcm_blocks_from_file_ffmpeg(source.path, config, volume=volume, on_debug=on_debug)
        return
    if codec != "python":
        raise ValueError("codec must be 'ffmpeg' or 'python'")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(msg.Error.VoiceFfmpegRequired)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source.path),
        "-ac",
        "1",
        "-ar",
        str(config.sample_rate),
        "-filter:a",
        f"volume={volume}",
        "-f",
        "s16le",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    encoder = ImaAdpcmEncoder()
    stats = _PcmStats()
    try:
        yield from adpcm_blocks_from_pcm_bytes(observe_pcm_blocks(iter_pcm_blocks(process.stdout, config.samples_per_block), stats), encoder)
    finally:
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        code = process.wait()
        if on_debug is not None:
            on_debug(
                "file pcm stats "
                f"blocks={stats.blocks} samples={stats.samples} peak={stats.peak} "
                f"silent={stats.peak == 0}"
            )
            if stderr.strip():
                on_debug("ffmpeg stderr " + " ".join(stderr.split())[:800])
        if code != 0:
            raise RuntimeError(msg.Error.VoiceFfmpegFailed.format(detail=stderr.strip() or f"exit code {code}"))


def adpcm_blocks_from_file_ffmpeg(
    file: str | Path,
    config: TalkConfig,
    *,
    volume: float = 1.0,
    on_debug: Callable[[str], None] | None = None,
) -> Iterator[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(msg.Error.VoiceFfmpegRequired)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(file),
        "-ac",
        "1",
        "-ar",
        str(config.sample_rate),
        "-filter:a",
        f"volume={volume}",
        "-c:a",
        "adpcm_ima_wav",
        "-block_size",
        str(config.block_align),
        "-f",
        "wav",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    try:
        yield from iter_wav_data_blocks(process.stdout, config.block_align)
    finally:
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        code = process.wait()
        if code != 0:
            raise RuntimeError(msg.Error.VoiceFfmpegFailed.format(detail=stderr.strip() or f"exit code {code}"))


def validate_audio_file(file: str | Path, *, on_debug: Callable[[str], None] | None = None) -> AudioFileInfo:
    path = Path(file)
    if not path.is_file():
        raise FileNotFoundError(msg.Error.VoiceFileNotFound.format(path=path))
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        if on_debug is not None:
            on_debug("ffprobe not found; file format check skipped")
        return AudioFileInfo(path, path.suffix.lstrip(".") or "unknown", "unknown", None, None, None)
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise RuntimeError(msg.Error.VoiceFfprobeFailed.format(detail=detail))
    try:
        info = audio_info_from_ffprobe(path, json.loads(result.stdout or "{}"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(msg.Error.VoiceFfprobeFailed.format(detail=str(exc))) from exc
    if on_debug is not None:
        duration = f"{info.duration:.3f}s" if info.duration is not None else "?"
        on_debug(
            "file input "
            f"format={info.format_name} codec={info.codec_name} "
            f"sample_rate={info.sample_rate or '?'} channels={info.channels or '?'} duration={duration}"
        )
    return info


def audio_info_from_ffprobe(path: Path, data: dict) -> AudioFileInfo:
    audio_stream = next((stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"), None)
    if audio_stream is None:
        raise ValueError(msg.Error.VoiceNoAudioStream.format(path=path))
    format_info = data.get("format") or {}
    duration = optional_float(audio_stream.get("duration") or format_info.get("duration"))
    sample_rate = optional_int(audio_stream.get("sample_rate"))
    channels = optional_int(audio_stream.get("channels"))
    return AudioFileInfo(
        path=path,
        format_name=str(format_info.get("format_name") or path.suffix.lstrip(".") or "unknown"),
        codec_name=str(audio_stream.get("codec_name") or "unknown"),
        sample_rate=sample_rate,
        channels=channels,
        duration=duration,
    )


def adpcm_blocks_from_microphone(config: TalkConfig, *, volume: float = 1.0, seconds: float | None = None) -> Iterator[bytes]:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(msg.Error.VoiceMicrophoneDependency) from exc

    q: queue.Queue[bytes] = queue.Queue(maxsize=30)

    def callback(indata, frames, time_info, status):  # noqa: ANN001, ARG001
        if status:
            pass
        try:
            q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    encoder = ImaAdpcmEncoder()
    started = time.monotonic()
    with sd.RawInputStream(
        samplerate=config.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=config.samples_per_block,
        callback=callback,
    ):
        while seconds is None or time.monotonic() - started < seconds:
            pcm = q.get()
            if volume != 1.0:
                pcm = scale_pcm16(pcm, volume)
            yield from adpcm_blocks_from_pcm_bytes([pcm], encoder)


def adpcm_blocks_from_tone(
    config: TalkConfig,
    *,
    frequency: float,
    seconds: float,
    volume: float,
) -> Iterator[bytes]:
    encoder = ImaAdpcmEncoder()
    total_samples = max(0, int(config.sample_rate * seconds))
    amplitude = int(32767 * max(0.0, min(1.0, volume)))
    offset = 0
    while offset < total_samples:
        count = min(config.samples_per_block, total_samples - offset)
        samples = [
            int(amplitude * math.sin(2.0 * math.pi * frequency * (offset + pos) / config.sample_rate))
            for pos in range(count)
        ]
        offset += count
        if samples:
            yield encoder.encode_block(samples)


def observe_pcm_blocks(blocks: Iterable[bytes], stats: _PcmStats) -> Iterator[bytes]:
    for block in blocks:
        stats.observe(block)
        yield block


def iter_pcm_blocks(stream, samples_per_block: int) -> Iterator[bytes]:  # noqa: ANN001
    block_bytes = samples_per_block * 2
    pending = bytearray()
    while True:
        chunk = stream.read(max(4096, block_bytes))
        if not chunk:
            break
        pending.extend(chunk)
        while len(pending) >= block_bytes:
            yield bytes(pending[:block_bytes])
            del pending[:block_bytes]
    if pending:
        yield bytes(pending)


def iter_wav_data_blocks(stream, block_align: int) -> Iterator[bytes]:  # noqa: ANN001
    header = read_exact(stream, 12)
    if header[:4] not in (b"RIFF", b"RF64") or header[8:12] != b"WAVE":
        raise RuntimeError(msg.Error.VoiceFfmpegFailed.format(detail="ffmpeg did not produce a WAV stream"))
    while True:
        chunk_header = read_exact(stream, 8)
        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack("<I", chunk_header[4:])[0]
        if chunk_id == b"data":
            if chunk_size == 0xFFFFFFFF:
                while True:
                    block = stream.read(block_align)
                    if not block:
                        return
                    if len(block) == block_align:
                        yield block
                return
            remaining = chunk_size
            while remaining > 0:
                size = min(block_align, remaining)
                block = read_exact(stream, size)
                remaining -= size
                if len(block) == block_align:
                    yield block
            if chunk_size % 2:
                stream.read(1)
            return
        skip = chunk_size + (chunk_size % 2)
        if skip:
            read_exact(stream, skip)


def read_exact(stream, size: int) -> bytes:  # noqa: ANN001
    data = stream.read(size)
    if len(data) != size:
        raise RuntimeError(msg.Error.VoiceFfmpegFailed.format(detail="unexpected end of ffmpeg output"))
    return data


def adpcm_blocks_from_pcm_bytes(chunks: Iterable[bytes], encoder: "ImaAdpcmEncoder") -> Iterator[bytes]:
    for chunk in chunks:
        samples = list(struct.unpack("<" + "h" * (len(chunk) // 2), chunk[: len(chunk) - len(chunk) % 2]))
        if samples:
            yield encoder.encode_block(samples)


def adpcm_block_duration(block: bytes, sample_rate: int) -> float:
    samples = max(1, (len(block) - 4) * 2 + 1)
    return samples / sample_rate


def adpcm_level_hint(block: bytes) -> int:
    return sum(((byte >> 4) & 0x07) + (byte & 0x07) for byte in block[4: min(len(block), 68)])


def optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def scale_pcm16(data: bytes, volume: float) -> bytes:
    samples = struct.unpack("<" + "h" * (len(data) // 2), data[: len(data) - len(data) % 2])
    scaled = [max(-32768, min(32767, int(sample * volume))) for sample in samples]
    return struct.pack("<" + "h" * len(scaled), *scaled)


def _required_int(root, tag: str) -> int:  # noqa: ANN001
    value = find_text(root, tag)
    if value is None:
        raise ProtocolError(msg.Error.VoiceNotSupported)
    return int(value)


class ImaAdpcmEncoder:
    """IMA ADPCM encoder used for camera talk audio."""

    def __init__(self) -> None:
        """Create an encoder with default predictor/index state."""
        self.predictor = 0
        self.index = 0
        self.initialized = False

    def encode_block(self, samples: list[int]) -> bytes:
        """
        Encode one PCM sample block as IMA ADPCM.

        :param samples: Signed 16-bit mono PCM samples.
        """

        if not samples:
            return b""
        if not self.initialized:
            self.index = 0
            self.initialized = True
        self.predictor = samples[0]
        header = struct.pack("<hBB", self.predictor, self.index, 0)
        nibbles = [self._encode_sample(sample) for sample in samples[1:]]
        packed = bytearray()
        for pos in range(0, len(nibbles), 2):
            high = nibbles[pos]
            low = nibbles[pos + 1] if pos + 1 < len(nibbles) else 0
            packed.append((high << 4) | low)
        return header + bytes(packed)

    def _encode_sample(self, sample: int) -> int:
        step = _IMA_STEP_TABLE[self.index]
        diff = sample - self.predictor
        code = 0
        if diff < 0:
            code = 8
            diff = -diff

        temp = step
        delta = step >> 3
        if diff >= temp:
            code |= 4
            diff -= temp
            delta += step
        temp >>= 1
        if diff >= temp:
            code |= 2
            diff -= temp
            delta += step >> 1
        temp >>= 1
        if diff >= temp:
            code |= 1
            delta += step >> 2

        if code & 8:
            self.predictor -= delta
        else:
            self.predictor += delta
        self.predictor = max(-32768, min(32767, self.predictor))
        self.index = max(0, min(88, self.index + _IMA_INDEX_TABLE[code]))
        return code & 0x0F
