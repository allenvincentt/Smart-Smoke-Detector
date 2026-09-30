"""BLE client for the ESP32 (GATT central). Runs bleak's asyncio loop on its own QThread so the
GUI thread never blocks; packets are parsed here and delivered to the UI through Qt signals.

The link must survive days of unattended running: every failure (BlueZ/D-Bus errors, adapter
off, ESP32 reboot, a connection that stays up but stops notifying) ends the session and the
loop rescans. Stopping cancels whatever is in flight, so the app closes promptly.
"""

import asyncio
import json
import logging
import time

from PySide6.QtCore import QThread, Signal

from app import config
from app.telemetry import COMMAND_UUID, DEVICE_NAME, SERVICE_UUID, TELEMETRY_UUID, Telemetry

log = logging.getLogger(__name__)

# Link states emitted on `link_state`. STALE = connected, but no packet for STALE_AFTER_S.
SCANNING, CONNECTING, LINKED, STALE, OFFLINE = "SCANNING", "CONNECTING", "LINKED", "STALE", "OFFLINE"

WRITE_TIMEOUT_S = 5.0


class BleLink(QThread):
    telemetry = Signal(object)   # Telemetry
    link_state = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._loop = None
        self._stop_event = None
        self._commands = None
        self._stopping = False
        self._state = OFFLINE
        self._last_rx = 0.0
        self._last_error = None

    # ---- called from the GUI thread
    def send_command(self, cmd: dict) -> bool:
        """Queue a firmware command, e.g. {"cmd": "reset"}. Returns False when not linked."""
        if self._state not in (LINKED, STALE) or self._loop is None:
            return False
        data = json.dumps(cmd, separators=(",", ":")).encode()
        self._loop.call_soon_threadsafe(self._enqueue, data)
        return True

    def stop(self) -> None:
        self._stopping = True
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass  # loop already closed
        if not self.wait(8000):
            log.warning("BLE thread did not stop in time")

    # ---- BLE thread
    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # noqa: BLE001 - never let the link thread die silently
            log.exception("BLE thread crashed")
            self._set_state(OFFLINE)

    def _enqueue(self, data: bytes) -> None:
        if not self._commands.full():
            self._commands.put_nowait(data)

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.link_state.emit(state)

    def _log_error(self, exc: BaseException) -> None:
        # A missing ESP32 or a powered-off adapter fails the same way every few seconds for as
        # long as it lasts: log each distinct error once, not every retry.
        msg = f"{type(exc).__name__}: {exc}"
        if msg != self._last_error:
            self._last_error = msg
            log.warning("BLE link error: %s", msg)

    async def _main(self) -> None:
        # Imported here so the GUI starts before bleak/D-Bus are loaded.
        from bleak import BleakClient, BleakScanner

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._commands = asyncio.Queue(maxsize=4)
        stopper = asyncio.ensure_future(self._stop_event.wait())

        while not self._stopping:
            session = asyncio.ensure_future(self._session(BleakScanner, BleakClient))
            await asyncio.wait((session, stopper), return_when=asyncio.FIRST_COMPLETED)
            if not session.done():  # stop requested mid-scan/connect: cancel it (disconnects)
                session.cancel()
            try:
                await session
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - BlueZ/D-Bus raise many types; always retry
                self._log_error(exc)
            if self._stopping:
                break
            self._set_state(OFFLINE)
            await asyncio.wait((stopper,), timeout=config.BLE_RETRY_S)
        stopper.cancel()

    async def _session(self, scanner_cls, client_cls) -> None:
        self._set_state(SCANNING)
        device = await scanner_cls.find_device_by_filter(
            lambda d, ad: d.name == DEVICE_NAME or SERVICE_UUID in ad.service_uuids,
            timeout=config.BLE_SCAN_S,
        )
        if device is None:
            return

        self._set_state(CONNECTING)
        async with client_cls(device) as client:
            while not self._commands.empty():  # never replay commands queued while offline
                self._commands.get_nowait()
            await client.start_notify(TELEMETRY_UUID, self._on_notify)
            self._last_rx = time.monotonic()
            self._on_notify(None, await client.read_gatt_char(TELEMETRY_UUID))
            self._set_state(LINKED)
            self._last_error = None

            while client.is_connected:
                try:
                    data = await asyncio.wait_for(self._commands.get(), 1.0)
                except asyncio.TimeoutError:
                    quiet = time.monotonic() - self._last_rx
                    if quiet > config.STALE_DROP_S:
                        # Connected but silent for too long (ESP32 hung, half-open link):
                        # drop the connection and rescan instead of staying STALE forever.
                        log.warning("No telemetry for %.0f s, reconnecting", quiet)
                        return
                    if quiet > config.STALE_AFTER_S:
                        self._set_state(STALE)
                    continue
                await asyncio.wait_for(client.write_gatt_char(COMMAND_UUID, data, response=True),
                                       WRITE_TIMEOUT_S)

    def _on_notify(self, _char, data: bytearray) -> None:
        t = Telemetry.parse(bytes(data))
        if t is None:
            return
        self._last_rx = t.rx
        if self._state == STALE:
            self._set_state(LINKED)
        self.telemetry.emit(t)
