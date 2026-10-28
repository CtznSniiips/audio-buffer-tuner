# Changelog

## [0.3.5-beta.1] - 2026-10-28
### Added
- **Multiselect group picker** on Dispatcharr builds with the new plugin settings system (v0.34 / current dev). One "Apply to these Channel Groups" field replaces the N single-select dropdowns, and the "Number of group filters" setting goes away. Settings are now split into collapsible "Channel groups" and "Buffer settings" sections.
- Detected at runtime: on v0.31–v0.33 the plugin keeps the original dropdown UI, so one release works on both.
- `manifest_version: 1` with the `proxy_internals` capability declared, so Dispatcharr's enable prompt says what the plugin touches. Builds before v0.34 ignore these keys.
- "Show current status" now reports from Redis: which Dispatcharr processes have the patch installed, and the last few overrides actually applied to a channel.

### Changed
- New Channel Groups now show up in the group picker automatically the next time the plugin page loads, on every supported Dispatcharr version. Before, Dispatcharr kept serving the group list from when the plugin loaded, so a new group only appeared after a plugin reload or a disable/re-enable. On v0.34+ the "Refresh group filters" action is removed because it's no longer needed.
- Upgrading keeps existing group selections. Groups picked in the old dropdowns stay active, and they're pre-selected in the multiselect, until you first save it.
### Changed
- New Channel Groups now show up in the group picker automatically the next time the plugin page loads, on every supported Dispatcharr version. Before, Dispatcharr kept serving the group list from when the plugin loaded, so a new group only appeared after a plugin reload or a disable/re-enable. On v0.34+ the "Refresh group filters" action is removed because it's no longer needed.

### Fixed
- The plugin key is no longer hardcoded. Dispatcharr's dev branch now keeps hyphens in marketplace install keys (`audio-buffer-tuner`), so the hardcoded `audio_buffer_tuner` lookup failed on fresh installs and the override silently never applied. The key now comes from the install folder, with both spellings as fallbacks.
- "Show current status" was misleading on v0.34+. Plugin actions now run on a Celery worker, so "patch installed in THIS process" was reporting the worker, not the uWSGI process that serves streams.

## [0.3.5] - 2026-09-16
### Fixed
- "Refresh group filters" action now actually forces Dispatcharr to reload the plugin (`discover_plugins(force_reload=True)`), instead of only updating an instance attribute the settings panel never read. Previously the action appeared to do something but had no real effect on its own.

## [0.3.4] - 2026-09-15
### Fixed
- Shortened the plugin description so it no longer gets cut off by Dispatcharr's 3-line card truncation.

### Changed
- Removed `name`/`version`/`description`/`author` from the `Plugin` class. Dispatcharr falls back to `plugin.json`'s values for any left unset, so that file is now the single source of truth for this metadata instead of two copies needing to stay in sync.

## [0.3.3] - 2026-09-15
### Fixed
- The "Refresh group filters" and "Show current status" actions now bypass the 5-second settings cache. Previously, running either action shortly after saving a setting could read a stale cached value and appear to do nothing.

## [0.3.2] - 2026-09-15
### Changed
- Default settings changed to `prebuffer_chunks = 2`, `chunk_size_kb = 128` (previously 1 / 64) — a safer starting point that still starts channels noticeably faster than Dispatcharr's stock defaults.
- Added `license`, `author`, and `min_dispatcharr_version` to `plugin.json` for public distribution.

### Fixed
- Channel lookups now key on `Channel.uuid`, not the integer `Channel.id` primary key. Every `live_proxy` call site passes the uuid around, so the override was silently never applying to any channel before this fix.

## [0.3.0] - 2026-09-15
### Added
- Chunk-size override (`chunk_size_kb`) in addition to the prebuffer chunk count — both apply only to channels in matching Channel Groups.

## [0.2.0] - 2026-09-15
### Changed
- Replaced the per-group checkbox list with a small, fixed number of single-select "group filter" dropdowns (configurable count), listing only Channel Groups that actually contain channels.

## [0.1.0] - 2026-09-15
### Added
- Initial release: per-Channel-Group override of Dispatcharr's TS-proxy initial prebuffer chunk count, to speed up startup for low-bitrate audio-only streams.
