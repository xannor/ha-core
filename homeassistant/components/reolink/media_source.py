"""Expose Reolink IP camera VODs as media sources."""

from __future__ import annotations

import datetime as dt
import logging

from aiohttp import ETag, web
from aiohttp.abc import AbstractStreamWriter
from aiohttp.typedefs import LooseHeaders
from aiohttp.web_request import BaseRequest
from reolink_aio.typings import VOD_download

from homeassistant.components.camera import DOMAIN as CAM_DOMAIN, DynamicStreamSettings
from homeassistant.components.http.view import HomeAssistantView
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.components.stream import create_stream
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import ReolinkData
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_get_media_source(hass: HomeAssistant) -> ReolinkVODMediaSource:
    """Set up camera media source."""
    hass.http.register_view(ReolinkDownloadView(hass))
    return ReolinkVODMediaSource(hass)


def res_name(stream: str) -> str:
    """Return the user friendly name for a stream."""
    return "High res." if stream == "main" else "Low res."


class ReolinkDownloadResponse(web.StreamResponse):
    """Stream passthrough/repeater similar in vein to web.FileResponse."""

    def __init__(
        self,
        source: VOD_download,
        *,
        status: int = 200,
        reason: str | None = None,
        headers: LooseHeaders | None = None,
    ) -> None:
        """Initialize response."""

        super().__init__(status=status, reason=reason, headers=headers)
        self._source = source

    async def prepare(self, request: BaseRequest) -> AbstractStreamWriter | None:
        """Prepare response."""
        source = self._source

        _LOGGER.debug("Preparing VOD for download (%s)", source.filename)

        if source.etag:
            self.etag = ETag(source.etag.replace('"', ""))
        self.content_length = source.length

        writer = await super().prepare(request)
        assert writer is not None

        transport = request.transport
        assert transport is not None

        try:
            async for chunk in source.stream.iter_any():
                if transport.is_closing():
                    _LOGGER.debug("Receiver closed stream, aborting download")
                    break
                await writer.write(chunk)
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.exception("Caught error during read. aborting")

        _LOGGER.debug("closing VOD")
        source.close()

        await writer.write_eof()
        return writer


class ReolinkDownloadView(HomeAssistantView):
    """Download VOD View."""

    url = "/api/reolink_download/{config_entry_id}/{channel_str}/{stream_res}/{filename}/{start}/{end}"
    name = "api:reolink:download"

    # requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize view."""
        super().__init__()
        self.data: dict[str, ReolinkData] = hass.data[DOMAIN]

    async def get(
        self,
        request: web.Request,
        config_entry_id: str,
        channel_str: str,
        stream_res: str,
        filename: str,
        start: str,
        end: str,
    ) -> web.StreamResponse:
        """Start a GET request."""

        entry = self.data.get(config_entry_id)
        if entry is None:
            raise web.HTTPNotFound()
        channel = int(channel_str)

        if entry.host.api.api_version("recDownload", channel) < 1:
            raise web.HTTPServiceUnavailable()

        source = await entry.host.api.download_vod(filename,start_time = start, end_time = end, channel = channel, stream=stream_res)

        return ReolinkDownloadResponse(source)


class ReolinkVODMediaSource(MediaSource):
    """Provide Reolink camera VODs as media sources."""

    name: str = "Reolink"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize ReolinkVODMediaSource."""
        super().__init__(DOMAIN)
        self.hass = hass
        self.data: dict[str, ReolinkData] = hass.data[DOMAIN]

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve media to a url."""
        identifier = item.identifier.split("|", 7)
        if identifier[0] != "FILE":
            raise Unresolvable(f"Unknown media item '{item.identifier}'.")

        _, config_entry_id, channel_str, stream_res, filename, start, end = identifier
        channel = int(channel_str)

        host = self.data[config_entry_id].host
        # NVR supports a download method, but it requires other api calls and consumes resources on the NVR to process
        # for now we will not do downloads on the NVR
        if host.api.api_version("recDownload", channel) == 1 and not host.api.is_nvr:
            return PlayMedia(
                ReolinkDownloadView.url.format(
                    config_entry_id=config_entry_id,
                    channel_str=channel_str,
                    stream_res=stream_res,
                    filename=filename,
                    start=start,
                    end=end,
                ),
                "video/mp4",
            )

        mime_type, url = await host.api.get_vod_source(channel, filename, stream_res)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            url_log = f"{url.split('&user=')[0]}&user=xxxxx&password=xxxxx"
            _LOGGER.debug(
                "Opening VOD stream from %s: %s", host.api.camera_name(channel), url_log
            )

        stream = create_stream(self.hass, url, {}, DynamicStreamSettings())
        stream.add_provider("hls", timeout=3600)
        stream_url: str = stream.endpoint_url("hls")
        stream_url = stream_url.replace("master_", "")
        return PlayMedia(stream_url, mime_type)

    async def async_browse_media(
        self,
        item: MediaSourceItem,
    ) -> BrowseMediaSource:
        """Return media."""
        if item.identifier is None:
            return await self._async_generate_root()

        identifier = item.identifier.split("|", 7)
        item_type = identifier[0]

        if item_type == "CAM":
            _, config_entry_id, channel_str = identifier
            return await self._async_generate_resolution_select(
                config_entry_id, int(channel_str)
            )
        if item_type == "RES":
            _, config_entry_id, channel_str, stream = identifier
            return await self._async_generate_camera_days(
                config_entry_id, int(channel_str), stream
            )
        if item_type == "DAY":
            (
                _,
                config_entry_id,
                channel_str,
                stream,
                year_str,
                month_str,
                day_str,
            ) = identifier
            return await self._async_generate_camera_files(
                config_entry_id,
                int(channel_str),
                stream,
                int(year_str),
                int(month_str),
                int(day_str),
            )

        raise Unresolvable(f"Unknown media item '{item.identifier}' during browsing.")

    async def _async_generate_root(self) -> BrowseMediaSource:
        """Return all available reolink cameras as root browsing structure."""
        children: list[BrowseMediaSource] = []

        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        for config_entry in self.hass.config_entries.async_entries(DOMAIN):
            if config_entry.state != ConfigEntryState.LOADED:
                continue
            channels: list[str] = []
            host = self.data[config_entry.entry_id].host
            entities = er.async_entries_for_config_entry(
                entity_reg, config_entry.entry_id
            )
            for entity in entities:
                if (
                    entity.disabled
                    or entity.device_id is None
                    or entity.domain != CAM_DOMAIN
                ):
                    continue

                device = device_reg.async_get(entity.device_id)
                ch = entity.unique_id.split("_")[1]
                if ch in channels or device is None:
                    continue
                channels.append(ch)

                if (
                    host.api.api_version("recReplay", int(ch)) < 1
                    and host.api.api_version("recDownload", int(ch)) < 1
                ) or not host.api.hdd_info:
                    # playback stream not supported by this camera or no storage installed
                    continue

                device_name = device.name
                if device.name_by_user is not None:
                    device_name = device.name_by_user

                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"CAM|{config_entry.entry_id}|{ch}",
                        media_class=MediaClass.CHANNEL,
                        media_content_type=MediaType.PLAYLIST,
                        title=device_name,
                        thumbnail=f"/api/camera_proxy/{entity.entity_id}",
                        can_play=False,
                        can_expand=True,
                    )
                )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.APP,
            media_content_type="",
            title="Reolink",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _async_generate_resolution_select(
        self, config_entry_id: str, channel: int
    ) -> BrowseMediaSource:
        """Allow the user to select the high or low playback resolution, (low loads faster)."""
        host = self.data[config_entry_id].host

        children: list[BrowseMediaSource] = []
        main_enc = await host.api.get_encoding(channel, "main")
        hasExt = not host.api.is_nvr and host.api.api_version("live", channel) > 0
        # NVR does not support downloads the same and cannot do RTSP so we will disable main on HD channels
        noMain = main_enc == "h265" and (
            host.api.api_version("recDownload", channel) < 1 or host.api.is_nvr
        )
        if noMain:
            _LOGGER.debug(
                "Reolink camera %s uses h265 encoding for main stream,"
                "playback only possible using sub stream",
                host.api.camera_name(channel),
            )
            if not hasExt:
                return await self._async_generate_camera_days(
                    config_entry_id, channel, "sub"
                )

        if hasExt:
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"RES|{config_entry_id}|{channel}|ext",
                    media_class=MediaClass.CHANNEL,
                    media_content_type=MediaType.PLAYLIST,
                    title="Middle resolution",
                    can_play=False,
                    can_expand=True,
                )
            )

        children.append(
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"RES|{config_entry_id}|{channel}|sub",
                media_class=MediaClass.CHANNEL,
                media_content_type=MediaType.PLAYLIST,
                title=f"Low resolution",
                can_play=False,
                can_expand=True,
            )
        )

        if not noMain:
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"RES|{config_entry_id}|{channel}|main",
                    media_class=MediaClass.CHANNEL,
                    media_content_type=MediaType.PLAYLIST,
                    title="High resolution",
                    can_play=False,
                    can_expand=True,
                ),
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"RESs|{config_entry_id}|{channel}",
            media_class=MediaClass.CHANNEL,
            media_content_type=MediaType.PLAYLIST,
            title=host.api.camera_name(channel),
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _async_generate_camera_days(
        self, config_entry_id: str, channel: int, stream: str
    ) -> BrowseMediaSource:
        """Return all days on which recordings are available for a reolink camera."""
        host = self.data[config_entry_id].host

        # We want today of the camera, not necessarily today of the server
        now = host.api.time() or await host.api.async_get_time()
        start = now - dt.timedelta(days=31)
        end = now

        children: list[BrowseMediaSource] = []
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Requesting recording days of %s from %s to %s",
                host.api.camera_name(channel),
                start,
                end,
            )
        statuses, _ = await host.api.request_vod_files(
            channel, start, end, status_only=True, stream=stream
        )
        for status in statuses:
            for day in status.days:
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"DAY|{config_entry_id}|{channel}|{stream}|{status.year}|{status.month}|{day}",
                        media_class=MediaClass.DIRECTORY,
                        media_content_type=MediaType.PLAYLIST,
                        title=f"{status.year}/{status.month}/{day}",
                        can_play=False,
                        can_expand=True,
                    )
                )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"DAYS|{config_entry_id}|{channel}|{stream}",
            media_class=MediaClass.CHANNEL,
            media_content_type=MediaType.PLAYLIST,
            title=f"{host.api.camera_name(channel)} {res_name(stream)}",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _async_generate_camera_files(
        self,
        config_entry_id: str,
        channel: int,
        stream: str,
        year: int,
        month: int,
        day: int,
    ) -> BrowseMediaSource:
        """Return all recording files on a specific day of a Reolink camera."""
        host = self.data[config_entry_id].host

        start = dt.datetime(year, month, day, hour=0, minute=0, second=0)
        end = dt.datetime(year, month, day, hour=23, minute=59, second=59)

        children: list[BrowseMediaSource] = []
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Requesting VODs of %s on %s/%s/%s",
                host.api.camera_name(channel),
                year,
                month,
                day,
            )
        _, vod_files = await host.api.request_vod_files(
            channel, start, end, stream=stream
        )
        for file in vod_files:
            file_name = f"{file.start_time.time()} {file.duration}"
            if file.triggers != file.triggers.NONE:
                file_name += " " + " ".join(
                    str(trigger.name).title()
                    for trigger in file.triggers
                    if trigger != trigger.NONE
                )

            file_start = file.start_time.strftime('%Y%m%d%H%M%S')
            file_end = file.end_time.strftime('%Y%m%d%H%M%S')
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"FILE|{config_entry_id}|{channel}|{stream}|{file.file_name}|{file_start}|{file_end}",
                    media_class=MediaClass.VIDEO,
                    media_content_type=MediaType.VIDEO,
                    title=file_name,
                    can_play=True,
                    can_expand=False,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"FILES|{config_entry_id}|{channel}|{stream}",
            media_class=MediaClass.CHANNEL,
            media_content_type=MediaType.PLAYLIST,
            title=f"{host.api.camera_name(channel)} {res_name(stream)} {year}/{month}/{day}",
            can_play=False,
            can_expand=True,
            children=children,
        )
