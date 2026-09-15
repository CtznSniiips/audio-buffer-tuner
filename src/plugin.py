"""
Audio Buffer Tuner — Dispatcharr plugin

Reduces the TS-proxy initial prebuffer (INITIAL_BEHIND_CHUNKS) *and*
optionally the per-chunk size (BUFFER_CHUNK_SIZE) for channels whose
Channel Group matches one of a small number of dropdown filters, without
touching Dispatcharr's own source.

Built against Dispatcharr v0.31.0 internals:
  - apps.proxy.config.TSConfig.INITIAL_BEHIND_CHUNKS is a plain class
    attribute (default 4, ~256KB/chunk).
  - apps.proxy.live_proxy.config_helper.ConfigHelper.initial_behind_chunks()
    is a staticmethod that reads it via getattr() and takes no arguments,
    so it has no idea which channel is asking.
  - apps.proxy.live_proxy.input.buffer.StreamBuffer.__init__ receives
    channel_id directly as a constructor argument (and always sets
    self.channel_id from it, called positionally or by keyword), and sets
    self.target_chunk_size once from BUFFER_CHUNK_SIZE. Unlike
    INITIAL_BEHIND_CHUNKS, this value is never re-read after construction,
    so a chunk-size override only affects buffers created after it's saved
    — not already-running channels.
  - ChannelGroup is shared between Channel.channel_group (related_name
    "channels") and Stream.channel_group (related_name "streams"), so a
    "group with streams but no channels" is a real thing to filter out.

Approach: replace both ConfigHelper.initial_behind_chunks and
StreamBuffer.__init__ with wrappers. The chunks wrapper walks a couple of
stack frames looking for `channel_id` (or self.buffer.channel_id); the
buffer wrapper needs no such trick since channel_id is a direct argument.
Both look up the channel's group id and only override behavior when that
id is one of the configured filter dropdowns' values. Every other
channel — and every failure mode below — falls straight back to
Dispatcharr's normal behavior:

  - plugin disabled -> never instantiated, no patch applied at all
  - no filter dropdown set to a group yet -> untouched
  - channel_id can't be found in the caller's frames (Dispatcharr changed
    something upstream) -> untouched
  - channel's group isn't one of the configured ones -> untouched
  - any exception anywhere in the lookup -> untouched

UI note: the settings panel's dropdown *options* (available groups) and
the *number* of dropdowns are both computed once when this Plugin class is
instantiated (i.e. when the plugin is enabled, or Dispatcharr restarts).
Dispatcharr's frontend only fetches the plugin field list once per page
load, so after changing "Number of group filters" or adding new Channel
Groups, reload the Dispatcharr page (or toggle this plugin off and back on)
to see the update — running the "Refresh channel group options" action
alone updates the backend's field list but won't retroactively refresh
what's already rendered in your browser tab.

This is a real monkeypatch of an internal, non-public function. It is
expected to need re-checking against Dispatcharr's source on major version
bumps — the frame-walking part in particular relies on internal variable
names that aren't a stable API.
"""

import sys
import time
import threading
import logging

from apps.proxy.live_proxy.config_helper import ConfigHelper
from apps.proxy.live_proxy.input.buffer import StreamBuffer
from apps.proxy.config import TSConfig
from apps.plugins.models import PluginConfig

logger = logging.getLogger(__name__)

PLUGIN_KEY = "audio_buffer_tuner"

_PATCH_FLAG = "_audio_buffer_tuner_patched"

MIN_FILTER_SLOTS = 1
MAX_FILTER_SLOTS = 20
DEFAULT_FILTER_SLOTS = 3

MIN_CHUNK_KB = 16
DEFAULT_CHUNK_KB = 128

DEFAULT_PREBUFFER_CHUNKS = 2

NONE_OPTION_VALUE = "none"  # "— none —" sentinel stored in a filter slot
# NOTE: must be non-blank — Dispatcharr's PluginFieldOptionSerializer.value
# is a plain CharField (allow_blank=False by default), so an option with
# value="" fails validation and silently drops the *entire* options list
# (and therefore the whole select field) with only a backend log warning.

# Keep real references to Dispatcharr's original implementations so we can
# always fall back to them, and so stop() can restore them cleanly.
_original_initial_behind_chunks = ConfigHelper.initial_behind_chunks
_original_stream_buffer_init = StreamBuffer.__init__

_settings_lock = threading.Lock()
_settings_cache = {"data": None, "ts": 0.0}
_SETTINGS_CACHE_TTL = 5  # seconds — mirrors TSConfig's own proxy-settings cache TTL

_group_lookup_lock = threading.Lock()
_group_lookup_cache = {}  # channel_id -> (group_id, cached_at)
_GROUP_LOOKUP_TTL = 30  # a channel's group rarely changes mid-stream

_log_dedup_lock = threading.Lock()
_logged_prebuffer_override = {}  # channel_id -> last-logged chunk value, to avoid
                                  # spamming INFO logs on every 0.5s poll while buffering


def _get_plugin_state(force_refresh=False):
    now = time.time()
    if not force_refresh:
        with _settings_lock:
            cached = _settings_cache["data"]
            if cached is not None and (now - _settings_cache["ts"]) < _SETTINGS_CACHE_TTL:
                return cached
    try:
        cfg = PluginConfig.objects.get(key=PLUGIN_KEY)
        data = {"enabled": bool(cfg.enabled), "settings": dict(cfg.settings or {})}
    except Exception:
        data = {"enabled": False, "settings": {}}
    with _settings_lock:
        _settings_cache["data"] = data
        _settings_cache["ts"] = now
    return data


def _channels_with_real_channels_qs():
    """ChannelGroup rows that actually back at least one Channel — excludes
    groups that only exist because a Stream (e.g. from an M3U group-title
    tag) points at them."""
    from apps.channels.models import ChannelGroup
    return ChannelGroup.objects.filter(channels__isnull=False).distinct().order_by("name")


def _channel_group_id(channel_id):
    """Look up a channel's group id, cached briefly, defensive on failure.

    NOTE: channel_id here is Channel.uuid, not the integer Channel.id primary
    key — every live_proxy call site (services/channel_service.py,
    input/manager.py, live_proxy/url_utils.py) passes the uuid around, so
    the lookup must filter on that field, not `id`.
    """
    now = time.time()
    with _group_lookup_lock:
        hit = _group_lookup_cache.get(channel_id)
        if hit is not None and (now - hit[1]) < _GROUP_LOOKUP_TTL:
            return hit[0]
    try:
        from apps.channels.models import Channel
        group_id = (
            Channel.objects.filter(uuid=channel_id)
            .values_list("channel_group_id", flat=True)
            .first()
        )
    except Exception:
        logger.debug("audio_buffer_tuner: channel group lookup failed", exc_info=True)
        return None
    with _group_lookup_lock:
        _group_lookup_cache[channel_id] = (group_id, now)
    return group_id


def _configured_group_ids(settings):
    """Collect the group ids chosen across all `group_filter_N` dropdowns."""
    ids = set()
    for i in range(1, MAX_FILTER_SLOTS + 1):
        raw = settings.get(f"group_filter_{i}")
        if raw and raw != NONE_OPTION_VALUE:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                pass
    return ids


def _find_channel_id_in_caller():
    """Best-effort: find the channel_id the *real* caller is working with.

    Dispatcharr's buffer-readiness call sites consistently have a
    `channel_id` local (or `self.buffer.channel_id`) at the point they call
    ConfigHelper.initial_behind_chunks(). If that ever stops being true
    after a Dispatcharr upgrade, this simply returns None and every channel
    falls back to the normal global default — it fails safe, not loud.
    """
    frame = sys._getframe(2)  # skip this function + _patched_initial_behind_chunks
    for _ in range(4):
        if frame is None:
            break
        loc = frame.f_locals
        cid = loc.get("channel_id")
        if cid:
            return cid
        self_obj = loc.get("self")
        buffer_obj = getattr(self_obj, "buffer", None)
        cid = getattr(buffer_obj, "channel_id", None)
        if cid:
            return cid
        frame = frame.f_back
    return None


def _patched_stream_buffer_init(self, *args, **kwargs):
    # Always run Dispatcharr's real init first — everything downstream
    # (Redis keys, buffer index, locks) depends on it having run normally.
    _original_stream_buffer_init(self, *args, **kwargs)
    try:
        state = _get_plugin_state()
        if not state["enabled"]:
            return

        settings = state["settings"]
        configured_ids = _configured_group_ids(settings)
        if not configured_ids:
            return

        channel_id = getattr(self, "channel_id", None)
        if channel_id is None:
            return  # e.g. the older HLS proxy path constructs StreamBuffer() with no channel_id

        group_id = _channel_group_id(channel_id)
        if group_id is None or group_id not in configured_ids:
            return

        default_bytes = getattr(self, "target_chunk_size", None)
        if not default_bytes:
            return

        try:
            chunk_kb = int(settings.get("chunk_size_kb", DEFAULT_CHUNK_KB))
        except (TypeError, ValueError):
            chunk_kb = DEFAULT_CHUNK_KB

        # Always at least MIN_CHUNK_KB, never larger than Dispatcharr's own default.
        chunk_kb = max(MIN_CHUNK_KB, min(chunk_kb, default_bytes // 1024))
        self.target_chunk_size = chunk_kb * 1024
        logger.info(
            f"audio_buffer_tuner: channel {channel_id} (group {group_id}) "
            f"chunk size override -> {self.target_chunk_size} bytes (default {default_bytes})"
        )
    except Exception:
        # Never let a bug in this plugin break stream buffering.
        logger.exception("audio_buffer_tuner: failed to apply chunk size override")


def _patched_initial_behind_chunks():
    try:
        state = _get_plugin_state()
        if not state["enabled"]:
            return _original_initial_behind_chunks()

        settings = state["settings"]
        configured_ids = _configured_group_ids(settings)
        if not configured_ids:
            return _original_initial_behind_chunks()

        channel_id = _find_channel_id_in_caller()
        if channel_id is None:
            return _original_initial_behind_chunks()

        group_id = _channel_group_id(channel_id)
        if group_id is None or group_id not in configured_ids:
            return _original_initial_behind_chunks()

        default_chunks = _original_initial_behind_chunks()
        try:
            chunks = int(settings.get("prebuffer_chunks", DEFAULT_PREBUFFER_CHUNKS))
        except (TypeError, ValueError):
            chunks = DEFAULT_PREBUFFER_CHUNKS

        # Always at least 1 chunk, never more than Dispatcharr's own default.
        chunks = max(1, min(chunks, default_chunks))
        with _log_dedup_lock:
            if _logged_prebuffer_override.get(channel_id) != chunks:
                _logged_prebuffer_override[channel_id] = chunks
                logger.info(
                    f"audio_buffer_tuner: channel {channel_id} (group {group_id}) "
                    f"prebuffer override -> {chunks} chunks (default {default_chunks})"
                )
                # Cheap unbounded-growth guard for long-running deployments with
                # lots of channel churn — this dict only exists for log dedup.
                if len(_logged_prebuffer_override) > 500:
                    _logged_prebuffer_override.clear()
        return chunks
    except Exception:
        # Never let a bug in this plugin break channel startup.
        logger.exception("audio_buffer_tuner: falling back to default behavior")
        return _original_initial_behind_chunks()


def _apply_patch():
    if getattr(ConfigHelper, _PATCH_FLAG, False):
        return
    ConfigHelper.initial_behind_chunks = staticmethod(_patched_initial_behind_chunks)
    StreamBuffer.__init__ = _patched_stream_buffer_init
    setattr(ConfigHelper, _PATCH_FLAG, True)
    logger.info("audio_buffer_tuner: patched ConfigHelper.initial_behind_chunks and StreamBuffer.__init__")


def _remove_patch():
    if not getattr(ConfigHelper, _PATCH_FLAG, False):
        return
    ConfigHelper.initial_behind_chunks = staticmethod(_original_initial_behind_chunks)
    StreamBuffer.__init__ = _original_stream_buffer_init
    setattr(ConfigHelper, _PATCH_FLAG, False)
    logger.info("audio_buffer_tuner: restored original ConfigHelper.initial_behind_chunks and StreamBuffer.__init__")

    def __init__(self):
        self.fields = self._build_fields()
        self.actions = [
            {
                "id": "refresh_groups",
                "label": "Refresh channel group options",
                "description": (
                    "Re-scan Channel Groups so newly added/renamed groups "
                    "are available to pick. Reload the Dispatcharr page "
                    "afterward to see the change — the settings panel only "
                    "loads field definitions once per page load."
                ),
            },
            {
                "id": "diagnose",
                "label": "Show current status",
                "description": (
                    "Reports whether the override is actually active in "
                    "this process, current settings, and which Channel "
                    "Groups exist — use this instead of digging through logs."
                ),
            },
        ]
        # Only reached when this plugin is enabled (Dispatcharr doesn't
        # instantiate disabled plugins), so it's safe to patch unconditionally.
        _apply_patch()

    def _build_fields(self):
        default_max = TSConfig.INITIAL_BEHIND_CHUNKS
        default_chunk_size_kb = TSConfig.BUFFER_CHUNK_SIZE // 1024
        default_prebuffer_kb = default_max * default_chunk_size_kb

        state = _get_plugin_state(force_refresh=True)
        try:
            slot_count = int(state["settings"].get("filter_slot_count", DEFAULT_FILTER_SLOTS))
        except (TypeError, ValueError):
            slot_count = DEFAULT_FILTER_SLOTS
        slot_count = max(MIN_FILTER_SLOTS, min(slot_count, MAX_FILTER_SLOTS))

        fields = [
            {
                "id": "prebuffer_chunks",
                "label": "Prebuffer chunks for matching groups",
                "type": "number",
                "min": 1,
                "max": default_max,
                "step": 1,
                "default": DEFAULT_PREBUFFER_CHUNKS,
                "help_text": (
                    f"Each chunk is ~{default_chunk_size_kb}KB. Dispatcharr's "
                    f"normal default is {default_max} chunks "
                    f"(~{default_prebuffer_kb}KB) before a channel goes "
                    f"active. Lower values start faster but leave less "
                    f"margin against provider hiccups — fine for steady "
                    f"low-bitrate audio, riskier for video."
                ),
            },
            {
                "id": "chunk_size_kb",
                "label": "Chunk size for matching groups (KB)",
                "type": "number",
                "min": MIN_CHUNK_KB,
                "max": default_chunk_size_kb,
                "step": 16,
                "default": DEFAULT_CHUNK_KB,
                "help_text": (
                    f"Dispatcharr's normal chunk size is ~{default_chunk_size_kb}KB. "
                    f"Smaller chunks mean less data has to accumulate before "
                    f"the first chunk (and each subsequent one) is ready, on "
                    f"top of whatever the prebuffer-chunk-count setting "
                    f"saves. Going very low increases Redis write frequency "
                    f"— usually fine for one or two audio channels, but "
                    f"don't push it to the floor across dozens of channels. "
                    f"Only applies to buffers created after you save this — "
                    f"already-running channels keep their current chunk "
                    f"size until they restart."
                ),
            },
            {
                "id": "filter_slot_count",
                "label": "Number of group filters",
                "type": "number",
                "min": MIN_FILTER_SLOTS,
                "max": MAX_FILTER_SLOTS,
                "step": 1,
                "default": DEFAULT_FILTER_SLOTS,
                "help_text": (
                    "How many group-picker dropdowns to show below. Increase "
                    "this, save, then reload the Dispatcharr page to see the "
                    "extra dropdowns."
                ),
            },
        ]

        try:
            groups = list(_channels_with_real_channels_qs().values("id", "name"))
        except Exception:
            logger.debug("audio_buffer_tuner: could not load channel groups yet", exc_info=True)
            groups = []

        if groups:
            options = [{"value": NONE_OPTION_VALUE, "label": "— none —"}] + [
                {"value": str(g["id"]), "label": g["name"]} for g in groups
            ]
            for i in range(1, slot_count + 1):
                fields.append(
                    {
                        "id": f"group_filter_{i}",
                        "label": f"Group filter {i}",
                        "type": "select",
                        "default": NONE_OPTION_VALUE,
                        "options": options,
                        "help_text": (
                            "Only groups that actually contain channels are "
                            "listed — groups that exist solely because a "
                            "Stream points at them are left out."
                        ),
                    }
                )
        else:
            fields.append(
                {
                    "id": "info_no_groups",
                    "type": "info",
                    "label": (
                        "No Channel Groups with actual channels found yet. "
                        "Save this plugin, then use \"Refresh channel group "
                        "options\" (and reload the page) once your channels exist."
                    ),
                }
            )

        return fields

    def run(self, action_id, params, context):
        if action_id == "refresh_groups":
            self.fields = self._build_fields()
            return {
                "status": "ok",
                "message": "Channel group options refreshed on the backend — reload the Dispatcharr page to see them.",
            }
        if action_id == "diagnose":
            return {"status": "ok", "file": self._diagnostic_report()}
        return {"status": "ok"}

    def _diagnostic_report(self):
        patched = getattr(ConfigHelper, _PATCH_FLAG, False)
        state = _get_plugin_state(force_refresh=True)
        settings = state["settings"]
        configured_ids = sorted(_configured_group_ids(settings))

        configured_group_filters = ", ".join(
            f"{i}={settings.get(f'group_filter_{i}', '(unset)')}"
            for i in range(1, MAX_FILTER_SLOTS + 1)
            if settings.get(f"group_filter_{i}")
        ) or "(none set)"

        lines = [
            f"Patch installed in THIS process: {patched}",
            f"Plugin enabled (per DB): {state['enabled']}",
            f"filter_slot_count setting: {settings.get('filter_slot_count', '(default)')}",
            f"prebuffer_chunks setting: {settings.get('prebuffer_chunks', '(default)')}",
            f"chunk_size_kb setting: {settings.get('chunk_size_kb', '(default)')}",
            f"Configured group_filter_N values: {configured_group_filters}",
            f"Group ids this would apply to: {configured_ids or '(none — override never fires)'}",
        ]
        try:
            groups = list(_channels_with_real_channels_qs().values("id", "name"))
            lines.append(
                "Eligible Channel Groups (id: name): "
                + (", ".join(f"{g['id']}:{g['name']}" for g in groups) or "(none found)")
            )
        except Exception as e:
            lines.append(f"Channel Group query failed: {e!r}")
        return " | ".join(lines)

    def stop(self, context):
        _remove_patch()
