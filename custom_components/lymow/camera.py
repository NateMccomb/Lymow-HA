"""Lymow camera platform: the diagnostic Map camera + the live RTSP/HLS stream camera.

The heavy map rendering lives in map_render.py; LymowMapCamera here just calls build_map_png().
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import tempfile
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .const import DOMAIN, F_IP_ADDRESS, F_NET_DETAIL, RTSP_PATH, RTSP_PORT
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity
from .map_render import build_map_png, png_bytes, text_png, safe_points, Image, _PIL_ERROR

_LOGGER = logging.getLogger(__name__)


def _get_robot_ip(data: dict) -> str | None:
    # netDetailInfo is stored as the raw protobuf message (no `.get()`); state.py
    # flattens wifiIp to a top-level key. Read the flat key, fall back to the object
    # via getattr — never `.get()` on the protobuf object (raised AttributeError).
    nd = data.get(F_NET_DETAIL)
    nd_wifi_ip = nd.get("wifiIp") if isinstance(nd, dict) else getattr(nd, "wifiIp", None)
    return (
        data.get(F_IP_ADDRESS)
        or data.get("wifiIp")
        or nd_wifi_ip
        or data.get("rest_ip_address")
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LymowMapCamera(coord), LymowRTSPCamera(coord)],
        update_before_add=False,
    )


# ── Diagnostic map camera ────────────────────────────────────────────────────

class LymowMapCamera(LymowEntity, Camera):
    _attr_name = "Map"
    _attr_icon = "mdi:map"
    _attr_content_type = "image/png"
    _attr_supported_features = CameraEntityFeature(0)

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "map")
        Camera.__init__(self)
        self._render_error: str | None = None
        self._render_debug: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    # ── sync render (always called in executor) ──────────────────

    def _render(self) -> bytes:
        try:
            imperial = self.hass.config.units is US_CUSTOMARY_SYSTEM
            name = (
                self.coordinator.config_entry.data.get("device_name")
                or (self.coordinator.device_info_data or {}).get("deviceName")
                or (self.coordinator.data or {}).get("deviceName")
                or "Lymow"
            )
            img, dbg = build_map_png(self.coordinator.data or {}, imperial=imperial, device_name=name)
            self._render_debug = dbg
            self._render_error = None
            return png_bytes(img)
        except Exception as exc:
            self._render_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.exception("Lymow diagnostic map render failed")
            return text_png("Lymow map render failed", self._render_error)

    # ── camera interface ─────────────────────────────────────────

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        # Sync path kept for compatibility; HA rarely calls this directly.
        return self._render()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        # Pillow is CPU-bound + blocking I/O → offload to executor thread.
        return await self.hass.async_add_executor_job(self._render)

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        btmap = d.get("btMap") or {}
        if not isinstance(btmap, dict):
            btmap = {}

        zones = btmap.get("zones") or []
        nogo_zones = btmap.get("nogoZones") or []
        channels = btmap.get("channels") or []
        _zname = {
            z.get("hashId"): z.get("name") for z in zones if isinstance(z, dict)
        }

        return {
            "render_mode": "diagnostic_png",
            "pil_available": Image is not None,
            "pil_error": _PIL_ERROR,
            "render_error": self._render_error,
            "render_debug": self._render_debug,
            "zone_count": btmap.get("zone_count"),
            "zones_total": len(zones),
            "zones_with_points": sum(1 for z in zones if z.get("points")),
            "drawable_zone_count": sum(
                1 for z in zones if z.get("points") and len(z.get("points") or []) >= 2
            ),
            "first_zone_points_count": len(zones[0].get("points") or []) if zones else 0,
            "channel_count": len(channels),
            "channels_detail": [
                {
                    "from": _zname.get(c.get("zone1")) or c.get("zone1"),
                    "to": _zname.get(c.get("zone2")) or c.get("zone2"),
                    "points": c.get("points_count", len(c.get("points") or [])),
                    "xy": [(round(x, 1), round(y, 1)) for x, y in safe_points(c.get("points") or [])],
                    "docking": bool(c.get("isDockingChannel")),
                    "detect_mode": c.get("detectMode"),
                    "cut_height": c.get("cutHeight"),
                    "channel_lift": c.get("channelLift"),
                }
                for c in channels
                if isinstance(c, dict)
            ],
            "nogo_zone_count": len(nogo_zones),
            "nogo_zones_with_points": sum(
                1 for z in nogo_zones
                if isinstance(z, dict) and z.get("points")
            ),
            "has_enu_base_point": bool(btmap.get("enuBasePoint") or d.get("enu_base_point")),
            # Per-zone mow config — cleanMode (3=cross/double), cleanDir (configured cut direction;
            # -1=optimized/auto), pathSpacing. Lets the pass classifier use the KNOWN directions.
            "zones_cfg": [
                {"name": z.get("name"),
                 "cleanMode": (z.get("zoneConfig") or {}).get("cleanMode"),
                 "cleanDir": (z.get("zoneConfig") or {}).get("cleanDir"),            # stripe angle
                 "relativeCleanDir": (z.get("zoneConfig") or {}).get("relativeCleanDir"),  # cross-cut angle
                 "perimeterMowDir": (z.get("zoneConfig") or {}).get("perimeterMowDir"),
                 "pathSpacing": (z.get("zoneConfig") or {}).get("pathSpacing")}
                for z in zones if isinstance(z, dict)
            ],
        }


# ── RTSP live camera ─────────────────────────────────────────────────────────

def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class LymowRTSPCamera(LymowEntity, Camera):
    """Live camera via FFmpeg HLS proxy → HA stream component.

    FFmpeg connects to the mower's RTSP stream with generous probe/analyze
    time (so it waits for the first IDR/SPS/PPS frame), transcodes to HLS
    segments served by a local HTTP server, and HA's stream component picks
    up the playlist.  This works around HA's hard-coded libav probe timeout
    which is too short for the mower's LIVE555 server.
    """

    _attr_name = "Live Camera"
    _attr_icon = "mdi:cctv"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: LymowCoordinator) -> None:
        LymowEntity.__init__(self, coordinator, "rtsp_camera")
        Camera.__init__(self)
        self.stream_options = {"rtsp_transport": "tcp"}
        self._proxy_process: asyncio.subprocess.Process | None = None
        self._http_server: HTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._hls_port: int | None = None
        self._hls_dir: str | None = None
        self._proxy_source_ip: str | None = None  # IP used when proxy was started

    @property
    def available(self) -> bool:
        return bool(_get_robot_ip(self.coordinator.data or {}))

    def _rtsp_url(self) -> str | None:
        ip = _get_robot_ip(self.coordinator.data or {})
        return f"rtsp://{ip}:{RTSP_PORT}/{RTSP_PATH}" if ip else None

    # ── lifecycle ────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Start proxy now if IP is already known; otherwise the coordinator
        # listener below will start it when data first arrives.
        if self._rtsp_url():
            await self._start_hls_proxy()
        # Listen for coordinator updates so we can (re)start when IP appears
        self.async_on_remove(
            self.coordinator.async_add_listener(self._async_on_coordinator_update)
        )

    async def async_will_remove_from_hass(self) -> None:
        await self._stop_hls_proxy()

    def _async_on_coordinator_update(self) -> None:
        """Restart proxy if the robot IP changed or proxy isn't running yet."""
        current_ip = _get_robot_ip(self.coordinator.data or {})
        if not current_ip:
            return
        if self._proxy_process is None or current_ip != self._proxy_source_ip:
            self.hass.async_create_task(self._restart_hls_proxy())

    async def _restart_hls_proxy(self) -> None:
        await self._stop_hls_proxy()
        await self._start_hls_proxy()

    # ── proxy management ─────────────────────────────────────────

    async def _start_hls_proxy(self) -> None:
        url = self._rtsp_url()
        if not url:
            _LOGGER.debug("Lymow camera: no robot IP, skipping HLS proxy start")
            return

        self._hls_dir = tempfile.mkdtemp(prefix="lymow_hls_")
        self._hls_port = _find_free_port()
        self._proxy_source_ip = _get_robot_ip(self.coordinator.data or {})

        # Tiny HTTP server that serves HLS segments from the temp dir
        hls_dir = self._hls_dir

        class _QuietHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=hls_dir, **kwargs)

            def log_message(self, format, *args):  # noqa: A002
                pass  # suppress per-request noise in HA logs

        self._http_server = HTTPServer(("127.0.0.1", self._hls_port), _QuietHandler)
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever, daemon=True, name="lymow_hls_http"
        )
        self._http_thread.start()
        _LOGGER.debug("Lymow HLS HTTP server started on port %s", self._hls_port)

        # FFmpeg: RTSP in (TCP, generous probe) → HLS out
        self._proxy_process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-analyzeduration", "3000000",   # 3 s — waits for first IDR frame
            "-probesize", "5000000",
            "-i", url,
            "-c:v", "copy",                  # no re-encode; just remux
            "-f", "hls",
            "-hls_time", "1",                # 1-second segments → low latency
            "-hls_list_size", "3",           # rolling 3-segment window
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", os.path.join(self._hls_dir, "seg%03d.ts"),
            os.path.join(self._hls_dir, "stream.m3u8"),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _LOGGER.debug(
            "Lymow FFmpeg HLS proxy started (pid=%s) → %s",
            self._proxy_process.pid,
            url,
        )

    async def _stop_hls_proxy(self) -> None:
        if self._proxy_process:
            with contextlib.suppress(ProcessLookupError):
                self._proxy_process.terminate()
                await self._proxy_process.wait()
            self._proxy_process = None

        if self._http_server:
            self._http_server.shutdown()
            self._http_server = None
            self._http_thread = None

        if self._hls_dir and os.path.isdir(self._hls_dir):
            shutil.rmtree(self._hls_dir, ignore_errors=True)
            self._hls_dir = None

        self._hls_port = None
        self._proxy_source_ip = None
        _LOGGER.debug("Lymow HLS proxy stopped")

    # ── camera interface ─────────────────────────────────────────

    async def stream_source(self) -> str | None:
        """HLS playlist served by local proxy, or raw RTSP as fallback."""
        if self._hls_port and self._hls_dir:
            return f"http://127.0.0.1:{self._hls_port}/stream.m3u8"
        return self._rtsp_url()

    @property
    def extra_state_attributes(self) -> dict:
        url = self._rtsp_url()
        attrs: dict = {"rtsp_url": url} if url else {}
        if self._hls_port:
            attrs["hls_proxy_url"] = f"http://127.0.0.1:{self._hls_port}/stream.m3u8"
        return attrs

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return text_png("Lymow Live Camera", self._rtsp_url() or "Waiting for robot IP…")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await self.hass.async_add_executor_job(self.camera_image, width, height)
