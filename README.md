# Audio Buffer Tuner

Reduces the TS-proxy's initial prebuffer for channels in a handful of chosen Channel Groups — cutting the time between a client requesting a channel and Dispatcharr going active. Built for steady, low-bitrate audio-only streams (internet radio, SiriusXM-style feeds), where Dispatcharr's default ~1MB buffer is far more margin than the stream actually needs.

## What it does

- Lowers **how many chunks** must arrive before a channel goes active (`INITIAL_BEHIND_CHUNKS`, default 4), and optionally **how big each chunk is** (`BUFFER_CHUNK_SIZE`, default ~256KB) — for channels in the Channel  Groups you pick, and only those.
- Both settings apply only when a channel's group matches one of a small, fixed number of single-select dropdowns (defaults to 3, adjustable). Only Channel Groups that actually back a real `Channel` are listed. Groups
  that exist solely because a `Stream` (e.g. an M3U group-title tag) points at them are left out.
- Video channels, and any channel in a group you haven't selected, are completely untouched. Any errors default back to Dispatcharr's normal defaults.

## Settings

- **Prebuffer chunks for matching groups** (default 2) and **Chunk size for matching groups (KB)** (default 128KB) — together these set the total bytes Dispatcharr waits for before a matching channel goes active. Lower  values start faster but leave less margin against provider hiccups: fine for a steady low-bitrate audio feed, risky for anything less stable.  Note the asymmetry — the chunk-count setting is re-checked live and takes  effect immediately, even mid-stream; the chunk-size setting is baked into a channel's buffer at creation time, so it only affects channels that (re)start after you save it.
- **Number of group filters** (default 3) — how many group-picker dropdowns to show. Change your `Number of group filters` setting, then Save. Press the plugin Actions button, then Run `Refresh group filters`. Refresh the page. The settings page will be updated with the set number of group-picker fields.
- **Show current status** (under Actions) — a diagnostic action that reports whether the override is actually active in this process, the current settings, and every eligible Channel Group. Use this before digging through logs if something doesn't seem to be applying.

## How it works, and what to know before relying on it

- This patches two internal, non-public Dispatcharr functions (`ConfigHelper.initial_behind_chunks` and `StreamBuffer.__init__`) rather than using a public plugin hook — Dispatcharr's proxy layer has no supported extension point for per-channel buffer tuning today. The patch is applied only while this plugin is enabled, and is cleanly reverted on disable.
- Determining *which* channel is asking for the prebuffer-chunk count relies on a `channel_id` local variable being present in the calling code's stack frame — true as of the Dispatcharr version this was built and tested against, but not a stable API. If a future Dispatcharr release changes that, this plugin simply stops discriminating by channel (falls back to the normal default everywhere) rather than breaking anything.
- After changing any setting in this plugin, Dispatcharr only picks up the change on the *next* channel start — an already-running channel keeps whatever buffer it was created with.

Built and verified against Dispatcharr v0.31.0.

## Benchmarks

Below are the results of some informal testing showing time measured in seconds for playback to start with different clients and various dispatcharr stream profiles.

|                                                           | Dispatcharr default prebuffer | With Audio Buffer Tuner: 2 Chunks, 128KB |
|-----------------------------------------------------------|:-----------------------------:|:----------------------------------------:|
| Player: VLC & Stream profile: Proxy                       | ~23 seconds                   | ~5 seconds                               |
| Player: VLC & Stream profile: default ffmpeg              | ~24 seconds                   | ~4 seconds                               |
| Player: VLC & Stream profile: **custom ffmpeg***          | ~21 seconds                   | ~3 seconds                               |
| Player: AdagioStream & Stream profile: Proxy              | ~38 seconds                   | ~9 seconds                               |
| Player: AdagioStream & Stream profile: default ffmpeg     | ~38 seconds                   | ~12 seconds                              |
| Player: AdagioStream & Stream profile: **custom ffmpeg*** | ~22 seconds                   | ~9 seconds                               |

***custom ffmpeg** stream profile used:  
```-user_agent {userAgent} -i {streamUrl} -vn -map 0:a:0 -c:a aac -b:a 256k -probesize 500000 -analyzeduration 1000000 -fflags +discardcorrupt+nobuffer -flags low_delay -af aresample=async=1 -muxdelay 0 -muxpreload 0 -f mpegts pipe:1```

> [!NOTE]
> * VLC tests done over local network
> * AdiagoStream tests done with the app default buffer setting (10s) via remote connection via Reverse Proxy
> * All tests completed using the same stream from the same source (bitrate 258kbps)

## License

MIT
