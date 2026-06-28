from __future__ import annotations

from typing import Any
import time

from .core.bc import ProtocolError
from .core.const import MSG, msg, payloads
from .internal.battery import normalize_mode, parse_battery_xml


class Battery:
    """Battery status helper returned by `Camera.battery()`."""

    def __init__(self, camera) -> None:
        """Create a battery helper.

        :param camera: Connected or connectable `Camera` instance.
        """
        self.camera = camera

    def raw(self, *, mode: str = "reconnect") -> str | None:
        """Return raw battery XML.

        :param mode: `reconnect` closes the connection after the request;
            `online` keeps the camera session active.
        """
        return self._request(mode=mode).xml_text

    def info(self, *, interval: float | None = None, count: int | None = None, mode: str = "reconnect"):
        """Return battery info once or an update iterator.

        :param interval: Poll interval in seconds. When omitted, a single
            `BatteryInfo` dict is returned.
        :param count: Optional maximum number of updates when `interval` is set.
        :param mode: `reconnect` or `online` polling mode.
        """
        if interval is None:
            return self.refresh(mode=mode)
        return BatteryInfoUpdates(self, interval=interval, count=count, mode=mode)

    def refresh(self, *, mode: str = "reconnect") -> dict[str, Any]:
        """Fetch one parsed battery status.

        :param mode: `reconnect` or `online` polling mode.
        """
        reply = self._request(mode=mode)
        if reply.header.response_code != 200:
            raise ProtocolError(msg.Error.BatteryInfoFailed.format(response_code=reply.header.response_code))
        return BatteryInfo(parse_battery_xml(reply.xml_root))

    def watch(self, interval: float = 60.0, *, count: int | None = None, mode: str = "reconnect"):
        """Yield parsed battery status repeatedly.

        :param interval: Delay between polls in seconds.
        :param count: Optional maximum number of updates.
        :param mode: `reconnect` or `online` polling mode.
        """
        with BatteryInfoUpdates(self, interval=interval, count=count, mode=mode) as updates:
            yield from updates

    def keepalive(self) -> str:
        """Send one keepalive/maintenance step through the camera."""
        return self.camera.keepalive()

    def _request(self, *, mode: str = "reconnect", retries: int = 1):
        mode = normalize_mode(mode)
        effective_online = mode == "online" or getattr(self.camera, "online_required", False)
        if not effective_online:
            self.camera.close()
        channel_id = self.camera.config.channel_id
        extension = payloads.extension.format(channel_id=channel_id)
        try:
            for attempt in range(retries + 1):
                try:
                    return self.camera.command(MSG.BATTERY, extension=extension)
                except (TimeoutError, EOFError, OSError):
                    if attempt >= retries:
                        raise
                    self.camera.reconnect()
            raise TimeoutError(msg.Error.BatteryRequestFailed)
        finally:
            if not effective_online:
                self.camera.close()


class BatteryInfoUpdates:
    """Iterator/context manager for repeated battery polling."""

    def __init__(
        self,
        battery: Battery,
        *,
        interval: float,
        count: int | None = None,
        mode: str = "reconnect",
        keepalive_interval: float = 1.0,
    ) -> None:
        """Create a battery polling iterator.

        :param battery: Battery helper used for requests.
        :param interval: Delay between updates in seconds.
        :param count: Optional maximum number of updates.
        :param mode: `reconnect` or `online` polling mode.
        :param keepalive_interval: Keepalive interval while waiting in online
            mode.
        """
        self.battery = battery
        self.interval = max(interval, 0.0)
        self.mode = normalize_mode(mode)
        self.keepalive_interval = max(keepalive_interval, 0.1)
        self.count = count
        self.seen = 0
        self.closed = False
        self._online_lease = None

    def __enter__(self) -> "BatteryInfoUpdates":
        self._enter_online_mode()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> "BatteryInfoUpdates":
        return self

    def __next__(self) -> dict[str, Any]:
        if self.closed or (self.count is not None and self.seen >= self.count):
            raise StopIteration
        self._enter_online_mode()
        if self.seen:
            self._wait()
        self.seen += 1
        return self.battery.refresh(mode=self.mode)

    def close(self) -> None:
        lease = getattr(self, "_online_lease", None)
        if lease is not None:
            lease.__exit__(None, None, None)
            self._online_lease = None
        self.closed = True

    def _enter_online_mode(self) -> None:
        if self.mode != "online" or getattr(self, "_online_lease", None) is not None:
            return
        require_online = getattr(self.battery.camera, "require_online", None)
        if require_online is None:
            return
        self._online_lease = require_online()
        self._online_lease.__enter__()

    def _wait(self) -> None:
        if self.mode == "reconnect" and not getattr(self.battery.camera, "online_required", False):
            time.sleep(self.interval)
            return
        deadline = time.monotonic() + self.interval
        while not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            sleep_for = min(remaining, self.keepalive_interval)
            time.sleep(sleep_for)
            if not self.closed:
                try:
                    self.battery.keepalive()
                except (TimeoutError, EOFError, OSError):
                    self.battery.camera.reconnect()


class BatteryInfo(dict):
    """Battery info dict that also works as a context manager."""

    def __enter__(self) -> "BatteryInfo":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


