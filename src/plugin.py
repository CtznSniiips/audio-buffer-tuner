"""
Audio Buffer Tuner — Dispatcharr plugin

Reduces the TS-proxy initial prebuffer (INITIAL_BEHIND_CHUNKS) *and*
optionally the per-chunk size (BUFFER_CHUNK_SIZE) for channels whose
Channel Group is one of the groups picked in this plugin's settings,
without touching Dispatcharr's own source.

Built against Dispatcharr v0.31.0 internals, re-verified against the
v0.34 dev branch (Oct 2026):
  - apps.proxy.config.TSConfig.INITIAL_BEHIND_CHUNKS is a plain class
    attribute (default 4, ~256KB/chunk).
  - apps.proxy.live_proxy.config_helper.ConfigHelper.initial_behind_chunks()
    is a staticmethod that takes no arguments, so it has no idea which
    channel is asking. Call sites: services/channel_service.py,
    input/manager.py (x2), output/ts/generator.py — all have either a
    `channel_id` local or `self.buffer.channel_id` in scope.
  - apps.proxy.live_proxy.input.buffer.StreamBuffer.__init__ receives
    channel_id directly as a constructor argument and sets
    self.target_chunk_size once from BUFFER_CHUNK_SIZE. That value is never
    re-read after construction, so a chunk-size override only affects
    buffers created after it's saved.
  - ChannelGroup is shared between Channel.channel_group (related_name
    "channels") and Stream.channel_group (related_name "streams"), so a
    "group with streams but no channels" is a real thing to filter out.

Approach: replace both ConfigHelper.initial_behind_chunks and
StreamBuffer.__init__ with wrappers. The chunks wrapper walks a couple of
stack frames looking for `channel_id` (or self.buffer.channel_id); the
buffer wrapper needs no such trick since channel_id is a direct argument.
Both only override behavior when the channel's group is one of the
configured groups. Every other channel — and every failure mode — falls
straight back to Dispatcharr's normal behavior.

Settings UI — two modes, picked at runtime:
  - Dispatcharr builds whose plugin field serializer knows `multiselect`
    (the "v3" plugin system, v0.34+ / current dev): one multiselect group
    picker, grouped into collapsible sections. Use the plugin page's own
    "Reload Plugin" button to pick up newly added Channel Groups.
  - Older builds (v0.31–v0.33): the original N single-select "group
    filter" dropdowns, with an adjustable count and a refresh action.
  Settings saved by the old dropdown mode are still honored in the new
  mode until the multiselect is saved for the first time (and they're
  pre-selected in it), so upgrading Dispatcharr doesn't silently drop
  anyone's group selection.

This is a real monkeypatch of an internal, non-public function. It is
expected to need re-checking against Dispatcharr's source on major version
bumps — the frame-walking part in particular relies on internal variable
names that aren't a stable API.
"""

import os
import sys
import json
import time
import socket
import threading
import logging

from apps.proxy.live_proxy.config_helper import ConfigHelper
from apps.proxy.live_proxy.input.buffer import StreamBuffer
from apps.proxy.config import TSConfig
from apps.plugins.models import PluginConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plugin key resolution
#
# Dispatcharr's registry key is the install folder name, lowercased with
# spaces -> "_" (apps/plugins/keys.py:normalize_plugin_key). What that folder
# is called depends on how the plugin was installed:
#   - manual drop / ZIP import: whatever the folder is called
#   - marketplace install on <= v0.33: slug with hyphens -> "_"
#     ("audio_buffer_tuner")
#   - marketplace install on the v0.34 dev branch: hyphens are now KEPT
#     ("audio-buffer-tuner"), unless an older config row already exists
# So a hardcoded key silently breaks fresh installs on newer builds (the
# PluginConfig lookup misses, the plugin reads as "disabled", and the
# override never fires). Derive it from our own folder instead, and fall
# back to the known spellings.
# ---------------------------------------------------------------------------

_KNOWN_KEYS = ("audio-buffer-tuner", "audio_buffer_tuner")


def _key_from_folder():
    try:
        folder = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
        return folder.replace(" ", "_").lower() or None
    except Exception:
        return None


_resolved_key = {"value": None}
_resolved_key_lock = threading.Lock()


def _plugin_key():
    """The PluginConfig.key Dispatcharr is actually using for this install."""
    with _resolved_key_lock:
        if _resolved_key["value"]:
            return _resolved_key["value"]
    candidates = []
    for k in (_key_from_folder(),) + _KNOWN_KEYS:
        if k and k not in candidates:
            candidates.append(k)
    found = None
    try:
        existing = set(
            PluginConfig.objects.filter(key__in=candidates).values_list("key", flat=True)
        )
        found = next((k for k in candidates if k in existing), None)
    except Exception:
        logger.debug("audio_buffer_tuner: plugin key lookup failed", exc_info=True)
    if found:
        with _resolved_key_lock:
            _resolved_key["value"] = found
        return found
    # Not cached: the DB may not be ready yet, try again next call.
    return candidates[0]


_PATCH_FLAG = "_audio_buffer_tuner_patched"

MIN_FILTER_SLOTS = 1
MAX_FILTER_SLOTS = 20
DEFAULT_FILTER_SLOTS = 3

MIN_CHUNK_KB = 16
DEFAULT_CHUNK_KB = 128

DEFAULT_PREBUFFER_CHUNKS = 2

GROUP_IDS_FIELD = "group_ids"  # multiselect (v3 settings UI)

NONE_OPTION_VALUE = "none"  # "— none —" sentinel stored in a legacy filter slot
# NOTE: must be non-blank — Dispatcharr's PluginFieldOptionSerializer.value
# is a plain CharField (allow_blank=False by default), so an option with
# value="" fails validation and silently drops the *entire* options list
# (and therefore the whole select field) with only a backend log warning.

# Redis keys for cross-process diagnostics. Plugin actions now run on a
# Celery worker (v0.34+), not in the uWSGI process that serves streams, so
# "is the patch installed in THIS process" no longer tells you anything
# useful from inside an action. Each patched process registers itself here,
# and every applied override is logged, so the status action can report
# what's happening where streams actually run.
_REDIS_PROCS_KEY = "plugin:audio_buffer_tuner:patched_procs"
_REDIS_EVENTS_KEY = "plugin:audio_buffer_tuner:recent_overrides"
_REDIS_EVENTS_MAX = 25
_REDIS_TTL = 7 * 24 * 3600

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


# ---------------------------------------------------------------------------
# Feature detection
# ---------------------------------------------------------------------------

def _supports_v3_fields():
    """True when this Dispatcharr build's plugin settings UI understands
    `multiselect` (and `section`). Older builds' field serializer rejects
    unknown types, which would silently drop the field entirely."""
    try:
        from apps.plugins.serializers import PluginFieldSerializer
        choices = PluginFieldSerializer().fields["type"].choices
        return "multiselect" in choices and "section" in choices
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Redis-backed diagnostics (best effort, never raises)
# ---------------------------------------------------------------------------

def _redis():
    try:
        from core.utils import RedisClient
        return RedisClient.get_client(max_retries=1, retry_interval=0)
    except Exception:
        return None


def _process_id():
    return f"{socket.gethostname()}:{os.getpid()}"


def _process_role():
    argv = " ".join(sys.argv).lower()
    if "celery" in argv:
        return "celery"
    if "uwsgi" in argv or "uwsgi" in sys.modules:
        return "uwsgi"
    if "daphne" in argv:
        return "daphne"
    return os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "unknown"


def _register_process(patched):
    r = _redis()
    if r is None:
        return
    try:
        if patched:
            r.hset(
                _REDIS_PROCS_KEY,
                _process_id(),
                json.dumps({"role": _process_role(), "since": int(time.time())}),
            )
            r.expire(_REDIS_PROCS_KEY, _REDIS_TTL)
        else:
            r.hdel(_REDIS_PROCS_KEY, _process_id())
    except Exception:
        logger.debug("audio_buffer_tuner: process registry update failed", exc_info=True)


def _record_override(kind, channel_id, group_id, value, default):
    r = _redis()
    if r is None:
        return
    try:
        r.lpush(
            _REDIS_EVENTS_KEY,
            json.dumps(
                {
                    "ts": int(time.time()),
                    "kind": kind,
                    "channel": str(channel_id),
                    "group": group_id,
                    "value": value,
                    "default": default,
                    "proc": _process_id(),
                }
            ),
        )
        r.ltrim(_REDIS_EVENTS_KEY, 0, _REDIS_EVENTS_MAX - 1)
        r.expire(_REDIS_EVENTS_KEY, _REDIS_TTL)
    except Exception:
        logger.debug("audio_buffer_tuner: override log write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Settings / lookups
# ---------------------------------------------------------------------------

def _get_plugin_state(force_refresh=False):
    """Return {'enabled': bool, 'settings': dict} for this plugin, cached briefly.

    We read straight from PluginConfig instead of relying on whatever the
    Plugin instance was constructed with, because settings changes made in
    the UI only reach a Plugin instance via run()/stop() — there's no live
    push into background code like this.

    force_refresh bypasses the cache entirely. Used by explicit, rare,
    user-initiated calls where stale-by-up-to-5-seconds data would be
    actively misleading. The cache stays in place for the hot path — the
    per-channel override checks — where it exists purely to avoid a DB hit
    on every 0.5s poll.
    """
    now = time.time()
    if not force_refresh:
        with _settings_lock:
            cached = _settings_cache["data"]
            if cached is not None and (now - _settings_cache["ts"]) < _SETTINGS_CACHE_TTL:
                return cached
    try:
        cfg = PluginConfig.objects.get(key=_plugin_key())
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
    key — every live_proxy call site passes the uuid around, so the lookup
    must filter on that field, not `id`.
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


def _legacy_group_ids(settings):
    """Group ids chosen across the old single-select `group_filter_N` dropdowns."""
    ids = set()
    for i in range(1, MAX_FILTER_SLOTS + 1):
        raw = settings.get(f"group_filter_{i}")
        if raw and raw != NONE_OPTION_VALUE:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                pass
    return ids


def _configured_group_ids(settings):
    """Collect the configured group ids.

    The multiselect value wins whenever it has been saved at all (even as an
    empty list — that means "user cleared it"). Until then, fall back to the
    legacy dropdown keys so an existing setup keeps working across a
    Dispatcharr upgrade. Dispatcharr v0.34+ drops settings keys with no
    matching field on the next save, so the legacy keys disappear on their
    own once the multiselect is saved.
    """
    if GROUP_IDS_FIELD in settings:
        raw = settings.get(GROUP_IDS_FIELD)
        ids = set()
        if isinstance(raw, (list, tuple)):
            for v in raw:
                try:
                    ids.add(int(v))
                except (TypeError, ValueError):
                    pass
        return ids
    return _legacy_group_ids(settings)


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


# ---------------------------------------------------------------------------
# Patches
# ---------------------------------------------------------------------------

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
        _record_override("chunk_size_bytes", channel_id, group_id, self.target_chunk_size, default_bytes)
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
            first_time = _logged_prebuffer_override.get(channel_id) != chunks
            if first_time:
                _logged_prebuffer_override[channel_id] = chunks
                # Cheap unbounded-growth guard for long-running deployments with
                # lots of channel churn — this dict only exists for log dedup.
                if len(_logged_prebuffer_override) > 500:
                    _logged_prebuffer_override.clear()
        if first_time:
            logger.info(
                f"audio_buffer_tuner: channel {channel_id} (group {group_id}) "
                f"prebuffer override -> {chunks} chunks (default {default_chunks})"
            )
            _record_override("prebuffer_chunks", channel_id, group_id, chunks, default_chunks)
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
    _register_process(True)
    logger.info("audio_buffer_tuner: patched ConfigHelper.initial_behind_chunks and StreamBuffer.__init__")


def _remove_patch():
    if not getattr(ConfigHelper, _PATCH_FLAG, False):
        return
    ConfigHelper.initial_behind_chunks = staticmethod(_original_initial_behind_chunks)
    StreamBuffer.__init__ = _original_stream_buffer_init
    setattr(ConfigHelper, _PATCH_FLAG, False)
    _register_process(False)
    logger.info("audio_buffer_tuner: restored original ConfigHelper.initial_behind_chunks and StreamBuffer.__init__")


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

_DIAGNOSE_ACTION = {
    "id": "diagnose",
    "label": "Show current status",
    "description": (
        "Reports which Dispatcharr processes have the override installed, "
        "current settings, the groups it applies to, and the most recent "
        "overrides actually applied to a channel."
    ),
}

_REFRESH_ACTION = {
    "id": "refresh_groups",
    "label": "Refresh group filters",
    "description": (
        "Rebuilds the group-picker dropdowns below: picks up newly "
        "added/renamed Channel Groups, AND applies any change to \"Number "
        "of group filters\". Run this after saving either kind of change, "
        "then reload the Dispatcharr page."
    ),
}


class Plugin:
    # name/version/description/author intentionally omitted — Dispatcharr
    # falls back to plugin.json's values for any of these left unset here,
    # so plugin.json is the single source of truth for this metadata.

    def __init__(self):
        self.v3_ui = _supports_v3_fields()
        self.fields = self._build_fields()
        # On v3 builds the plugin page has its own "Reload Plugin" button,
        # which re-runs this __init__ (fresh group list) AND refetches the
        # field list in the browser — strictly better than our own refresh
        # action, which also needed a manual page reload.
        self.actions = [_DIAGNOSE_ACTION] if self.v3_ui else [_REFRESH_ACTION, _DIAGNOSE_ACTION]
        # Only reached when this plugin is enabled (Dispatcharr doesn't
        # instantiate disabled plugins), so it's safe to patch unconditionally.
        _apply_patch()

    # -- fields -------------------------------------------------------------

    def _buffer_fields(self, section=None):
        default_max = TSConfig.INITIAL_BEHIND_CHUNKS
        default_chunk_size_kb = TSConfig.BUFFER_CHUNK_SIZE // 1024
        default_prebuffer_kb = default_max * default_chunk_size_kb
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
        ]
        if section:
            for f in fields:
                f["section"] = section
        return fields

    def _load_groups(self):
        try:
            return list(_channels_with_real_channels_qs().values("id", "name"))
        except Exception:
            logger.debug("audio_buffer_tuner: could not load channel groups yet", exc_info=True)
            return []

    def _build_fields(self):
        return self._build_fields_v3() if self.v3_ui else self._build_fields_legacy()

    def _build_fields_v3(self):
        state = _get_plugin_state(force_refresh=True)
        settings = state["settings"]
        groups = self._load_groups()
        option_values = {str(g["id"]) for g in groups}

        # Pre-select whatever the old dropdowns had chosen, so the first save
        # after upgrading carries the selection over. Only ids that are still
        # valid options — Dispatcharr rejects a save containing an unknown
        # multiselect value.
        legacy_default = sorted(
            (str(i) for i in _legacy_group_ids(settings) if str(i) in option_values),
            key=int,
        )

        fields = [
            {"id": "sec_groups", "label": "Channel groups", "type": "section"},
        ]
        if groups:
            fields.append(
                {
                    "id": GROUP_IDS_FIELD,
                    "label": "Apply to these Channel Groups",
                    "type": "multiselect",
                    "section": "sec_groups",
                    "default": legacy_default,
                    "placeholder": "Pick one or more groups",
                    "options": [{"value": str(g["id"]), "label": g["name"]} for g in groups],
                    "help_text": (
                        "Only groups that actually contain channels are "
                        "listed — groups that exist solely because a Stream "
                        "points at them are left out. Added a group since "
                        "enabling this plugin? Use Reload Plugin at the top "
                        "of this page to refresh the list."
                    ),
                }
            )
        else:
            fields.append(
                {
                    "id": "info_no_groups",
                    "type": "info",
                    "section": "sec_groups",
                    "label": (
                        "No Channel Groups with actual channels found yet. "
                        "Once your channels exist, use Reload Plugin at the "
                        "top of this page to populate the group picker."
                    ),
                }
            )
        fields.append({"id": "sec_buffer", "label": "Buffer settings", "type": "section"})
        fields.extend(self._buffer_fields(section="sec_buffer"))
        return fields

    def _build_fields_legacy(self):
        state = _get_plugin_state(force_refresh=True)
        try:
            slot_count = int(state["settings"].get("filter_slot_count", DEFAULT_FILTER_SLOTS))
        except (TypeError, ValueError):
            slot_count = DEFAULT_FILTER_SLOTS
        slot_count = max(MIN_FILTER_SLOTS, min(slot_count, MAX_FILTER_SLOTS))

        fields = self._buffer_fields()
        fields.append(
            {
                "id": "filter_slot_count",
                "label": "Number of group filters",
                "type": "number",
                "min": MIN_FILTER_SLOTS,
                "max": MAX_FILTER_SLOTS,
                "step": 1,
                "default": DEFAULT_FILTER_SLOTS,
                "help_text": (
                    "How many group-picker dropdowns to show below. After "
                    "changing this, save, run the \"Refresh group "
                    "filters\" action (Actions tab), then reload the "
                    "Dispatcharr page — saving alone does not regenerate "
                    "the dropdowns."
                ),
            }
        )

        groups = self._load_groups()
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
                        "Save this plugin, then use \"Refresh group "
                        "filters\" (and reload the page) once your channels exist."
                    ),
                }
            )
        return fields

    # -- actions ------------------------------------------------------------

    def run(self, action_id, params, context):
        if action_id == "refresh_groups" and not self.v3_ui:
            self.fields = self._build_fields()
            try:
                # self.fields alone is cosmetic: the plugin list serves a
                # registry snapshot frozen at the last (re)load. Forcing a
                # re-discovery regenerates it.
                from apps.plugins.loader import PluginManager
                PluginManager.get().discover_plugins(force_reload=True)
                message = "Group filters rebuilt — reload the Dispatcharr page to see them."
            except Exception:
                logger.exception("audio_buffer_tuner: forced re-discovery failed")
                message = (
                    "Group filters rebuilt, but the forced reload failed — "
                    "toggle the plugin off and back on to pick up the change."
                )
            return {"status": "ok", "message": message}
        if action_id == "refresh_groups":
            return {
                "status": "ok",
                "message": "Use the Reload Plugin button at the top of this page to refresh the group list.",
            }
        if action_id == "diagnose":
            # "file" is rendered persistently as "Output: ..." under the
            # action row; "message" is the transient toast.
            return {"status": "ok", "message": "Status report shown below the action.", "file": self._diagnostic_report()}
        return {"status": "ok"}

    def _diagnostic_report(self):
        state = _get_plugin_state(force_refresh=True)
        settings = state["settings"]
        configured_ids = sorted(_configured_group_ids(settings))
        source = (
            "multiselect" if GROUP_IDS_FIELD in settings
            else ("legacy dropdowns" if _legacy_group_ids(settings) else "none")
        )

        try:
            names = dict(_channels_with_real_channels_qs().values_list("id", "name"))
        except Exception:
            names = {}

        lines = [
            f"Plugin key: {_plugin_key()}",
            f"Settings UI: {'v3 (multiselect)' if self.v3_ui else 'legacy dropdowns'}",
            f"Enabled (per DB): {state['enabled']}",
            f"prebuffer_chunks: {settings.get('prebuffer_chunks', f'(default {DEFAULT_PREBUFFER_CHUNKS})')}",
            f"chunk_size_kb: {settings.get('chunk_size_kb', f'(default {DEFAULT_CHUNK_KB})')}",
            "Applies to groups ("
            + source
            + "): "
            + (", ".join(f"{gid}:{names.get(gid, '?')}" for gid in configured_ids) or "(none — override never fires)"),
        ]

        r = _redis()
        if r is None:
            lines.append("Redis unavailable — can't report cross-process status")
        else:
            try:
                procs = r.hgetall(_REDIS_PROCS_KEY) or {}
                proc_desc = []
                for pid, raw in procs.items():
                    pid = pid.decode() if isinstance(pid, bytes) else pid
                    try:
                        info = json.loads(raw)
                        proc_desc.append(f"{pid} ({info.get('role', '?')})")
                    except Exception:
                        proc_desc.append(pid)
                lines.append(
                    "Processes with patch installed: "
                    + (", ".join(sorted(proc_desc)) or "(none registered)")
                )
            except Exception as e:
                lines.append(f"Process registry read failed: {e!r}")
            try:
                events = r.lrange(_REDIS_EVENTS_KEY, 0, 4) or []
                ev_desc = []
                for raw in events:
                    try:
                        ev = json.loads(raw)
                        age = int(time.time()) - int(ev.get("ts", 0))
                        ev_desc.append(
                            f"{ev.get('kind')}={ev.get('value')} (default {ev.get('default')}) "
                            f"ch {str(ev.get('channel'))[:8]}… grp {ev.get('group')} {age}s ago"
                        )
                    except Exception:
                        continue
                lines.append(
                    "Recent overrides: " + ("; ".join(ev_desc) or "(none yet — start a matching channel)")
                )
            except Exception as e:
                lines.append(f"Override log read failed: {e!r}")

        return " | ".join(lines)

    def stop(self, context):
        _remove_patch()
