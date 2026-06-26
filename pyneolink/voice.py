from __future__ import annotations

import time
from pathlib import Path
from collections.abc import Callable, Iterable

from .core.bc import ProtocolError
from .core.const import AUDIO_PLAY, MSG, msg, payloads
from .internal.voice import (
    TalkConfig,
    adpcm_block_duration as _adpcm_block_duration,
    adpcm_blocks_from_file as _adpcm_blocks_from_file,
    adpcm_blocks_from_microphone as _adpcm_blocks_from_microphone,
    adpcm_blocks_from_tone as _adpcm_blocks_from_tone,
    adpcm_level_hint as _adpcm_level_hint,
    parse_talk_config,
    serialize_bcmedia_adpcm,
    talk_config_payload,
)


class Voice:
    def __init__(self, camera) -> None:
        self.camera = camera
        self._lease = None
        self._started = False
        self._last_talk_msg_num: int | None = None

    def __enter__(self) -> "Voice":
        self._lease = self.camera.require_online()
        self._lease.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None

    def ability(self) -> TalkConfig:
        reply = self.camera.command(MSG.TALKABILITY, extension=payloads.extension.format(channel_id=self.camera.config.channel_id))
        if reply.header.response_code != 200:
            raise ProtocolError(msg.Error.Response.format(response_code=reply.header.response_code))
        if reply.xml_text:
            self._debug("talk ability " + " ".join(reply.xml_text.split())[:800])
        return parse_talk_config(reply.xml_root, channel_id=self.camera.config.channel_id)

    def play(
        self,
        file: str | Path,
        *,
        volume: float = 1.0,
        codec: str = "python",
        wait_ack: bool = False,
        on_ready: Callable[[TalkConfig], None] | None = None,
    ) -> None:
        with self.camera.require_online():
            config = self.ability()
            self._start(config)
            if on_ready is not None:
                on_ready(config)
            try:
                self._send_blocks(
                    _adpcm_blocks_from_file(file, config, volume=volume, codec=codec, on_debug=self._debug),
                    config,
                    wait_ack=wait_ack,
                )
            finally:
                self.stop()

    def tone(
        self,
        *,
        frequency: float = 1000.0,
        seconds: float = 3.0,
        volume: float = 0.4,
        wait_ack: bool = False,
        on_ready: Callable[[TalkConfig], None] | None = None,
    ) -> None:
        with self.camera.require_online():
            config = self.ability()
            self._start(config)
            if on_ready is not None:
                on_ready(config)
            try:
                self._send_blocks(
                    _adpcm_blocks_from_tone(config, frequency=frequency, seconds=seconds, volume=volume),
                    config,
                    wait_ack=wait_ack,
                )
            finally:
                self.stop()

    def microphone(
        self,
        *,
        volume: float = 1.0,
        seconds: float | None = None,
        wait_ack: bool = False,
        on_ready: Callable[[TalkConfig], None] | None = None,
    ) -> None:
        with self.camera.require_online():
            config = self.ability()
            self._start(config)
            if on_ready is not None:
                on_ready(config)
            try:
                self._send_blocks(_adpcm_blocks_from_microphone(config, volume=volume, seconds=seconds), config, wait_ack=wait_ack)
            finally:
                self.stop()

    def siren(self) -> None:
        self._siren_command()

    def stop(self, *, wait: bool = False, force: bool = False) -> None:
        if not self._started and not force:
            return
        try:
            self._drain_talk_replies()
            msg_num = self.camera.send(MSG.TALKRESET, extension=payloads.extension.format(channel_id=self.camera.config.channel_id))
            if wait:
                self._wait_for_stop_reply(msg_num)
        except Exception:
            pass
        finally:
            self._started = False
            self._last_talk_msg_num = None

    def _start(self, config: TalkConfig) -> None:
        if config.audio_type != "adpcm":
            raise ProtocolError(msg.Error.VoiceNeedsAdpcm)
        reply = self.camera.command(MSG.TALKCONFIG, talk_config_payload(config), extension=payloads.extension.format(channel_id=config.channel_id))
        if reply.header.response_code == 422:
            self.stop(force=True)
            reply = self.camera.command(MSG.TALKCONFIG, talk_config_payload(config), extension=payloads.extension.format(channel_id=config.channel_id))
        if reply.header.response_code != 200:
            raise ProtocolError(msg.Error.Response.format(response_code=reply.header.response_code))
        self._debug(
            "talk config accepted "
            f"sample_rate={config.sample_rate} block_align={config.block_align} "
            f"length_per_encoder={config.length_per_encoder}"
        )
        self._started = True

    def _send_blocks(self, blocks: Iterable[bytes], config: TalkConfig, *, wait_ack: bool = False) -> None:
        msg_num = self.camera._next_msg()
        self._last_talk_msg_num = msg_num
        next_play_end = time.monotonic()
        sent = 0
        first_sent_at: float | None = None
        for block in blocks:
            packet = serialize_bcmedia_adpcm(block)
            sent_at = time.monotonic()
            first_sent_at = sent_at if first_sent_at is None else first_sent_at
            self.camera.send(
                MSG.TALK,
                packet,
                extension=payloads.extension_binary_data.format(channel_id=config.channel_id),
                msg_num=msg_num,
            )
            response = "skipped"
            if wait_ack:
                reply = self.camera._recv_matching(MSG.TALK, msg_num)
                response = str(reply.header.response_code)
                if reply.header.response_code not in (0, 200):
                    raise ProtocolError(msg.Error.Response.format(response_code=reply.header.response_code))
            sent += 1
            if sent <= 5 or sent % 25 == 0:
                self._debug(
                    f"talk chunk #{sent} block={len(block)} packet={len(packet)} "
                    f"reply={response} level={_adpcm_level_hint(block)} block_head={block[:12].hex()}"
                )
            play_seconds = _adpcm_block_duration(block, config.sample_rate)
            next_play_end = max(next_play_end, sent_at) + play_seconds
            sleep_for = next_play_end - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
        elapsed = time.monotonic() - first_sent_at if first_sent_at is not None else 0.0
        self._debug(f"talk chunks sent={sent} elapsed={elapsed:.3f}s")

    def _drain_talk_replies(self, *, seconds: float = 0.25) -> None:
        if self._last_talk_msg_num is None:
            return
        deadline = time.monotonic() + seconds
        drained = 0
        while time.monotonic() < deadline:
            try:
                reply = self.camera._recv(timeout=0.02)
            except TimeoutError:
                break
            if reply.header.msg_id == MSG.TALK and reply.header.msg_num == self._last_talk_msg_num:
                drained += 1
        if drained:
            self._debug(f"drained {drained} old talk replies before stop")

    def _wait_for_stop_reply(self, msg_num: int) -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                reply = self.camera._recv(timeout=0.1)
            except TimeoutError:
                return
            if reply.header.msg_id == MSG.TALKRESET and reply.header.msg_num == msg_num:
                return

    def _siren_command(self) -> None:
        payload = payloads.audio_play_info.format(
            channel_id=self.camera.config.channel_id,
            play_mode=AUDIO_PLAY.SIREN_MODE,
            play_duration=0,
            play_times=AUDIO_PLAY.DEFAULT_TIMES,
            on_off=AUDIO_PLAY.SIREN_TRIGGER,
        )
        reply = self.camera.command(MSG.PLAY_AUDIO, payload, extension=payloads.extension.format(channel_id=self.camera.config.channel_id))
        if reply.header.response_code not in (0, 200):
            raise ProtocolError(msg.Error.Response.format(response_code=reply.header.response_code))
        self._debug("siren command accepted")

    def _debug(self, text: str) -> None:
        if getattr(self.camera, "debug", False):
            print(msg.Log.Pyneolink.format(message=f"voice: {text}"))


__all__ = [
    "TalkConfig",
    "Voice",
]
