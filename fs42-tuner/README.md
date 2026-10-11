# fs42-tuner

The tuner and dashboard of [Station Wrapper 43](../README.md): it turns what [FieldStation42](https://github.com/shane-mason/FieldStation42) has scheduled into one live MPEG-TS stream per channel, presents them as an HDHomeRun tuner with an XMLTV guide (for Jellyfin and other IPTV clients), and serves the dashboard and web player. This file covers how the tuner itself works. Installing and using it is in the [README](../README.md) and [docs/REFERENCE.md](../docs/REFERENCE.md). No display, no X11, no mpv, no screen scraping.

## How it works

FieldStation42 flattens every programming block into a **plan** before anything plays: a flat list of `(path, skip, duration)` entries where the show, its commercials and its bumps sit side by side. The plan is stored in the `liquid_blocks` table of FieldStation42's sqlite database. The schedule is fully resolved ahead of time, so "what is channel 3 playing at 8:17:23 pm, and how many seconds into which file" is a lookup, not a simulation.

`resolver.py` reimplements FieldStation42's own `LiquidManager.get_play_point()` against that table, and `tuner.py` wraps it in an HDHomeRun-shaped HTTP service. FieldStation42 itself keeps running (it owns the catalog, the scheduling rules and the console); it just never opens a window, because `field_player.py` is not run.

```
FieldStation42 --writes--> liquid_blocks (sqlite) --reads--> tuner --MPEG-TS--> Jellyfin, VLC, the web player
  (scheduler)                                                (ffmpeg)
```

The tuner and the scheduler must agree on the time, or the tuner streams the wrong programme. They run in one container, so they do (the compose file mounts `/etc/localtime`, or sets `TZ`).

### Viewers vs. encoders

A real HDHomeRun burns one physical tuner per viewer. That doesn't apply here: a channel is a pure function of wall-clock time, so **a channel is encoded once and fanned out to everyone on it**. Two limits, in `tuner/config.json`:

| setting | default | what it caps |
|---|---|---|
| `max_encoders` | 4 | distinct channels live at once (the real CPU cost, about one core per 720p encode) |
| `tuner_count` | 12 | client connections, advertised to Jellyfin as `TunerCount` |

With 4 and 12, twelve people can watch in any mix as long as they are on four or fewer distinct channels: a fifth *distinct* channel gets a 503, while a thirteenth viewer on a running channel is refused only by `tuner_count`. A channel's encoder stops `linger_seconds` (10) after its last viewer leaves. This is why it presents as an HDHomeRun rather than a plain M3U: `discover.json` carries `TunerCount`, which is how a client learns the limit.

### Late joiners

Each channel keeps a rolling buffer of its last 3 seconds of output. A new viewer is handed it on arrival, in blocks of 64 KB, so the decoder gets PAT/PMT and a keyframe at once instead of waiting up to a GOP. Keyframes are on a fixed 2 second grid (`-g fps*2`, `sc_threshold 0`). A slow client gets a bounded queue and is dropped when it overflows, rather than back-pressuring the shared encoder.

### Segments

Each plan entry is one ffmpeg run, every one normalised to the same encoder parameters (the failure mode of stitching mixed files into a TS stream is a glitch at every boundary), with timestamps carried on from the last segment. `-ss` goes before `-i` so a join mid-file is fast. Test patterns for a missing file, a bad file, an empty break or nothing scheduled are made the same way, with their reason drawn in the bottom right corner. Hardware encoding (`h264_vaapi`, `h264_nvenc`, `h264_qsv`) is a `vcodec` change in the video settings of the config.

### Captions

A video's captions (a subtitle file beside it, or a text track inside it) are written into the stream as CEA-608 by `ccinject.py`, which sits between the encoder and the viewers and adds the A/53 caption data to each frame's access unit. The encoder runs without B-frames for those segments, so frames are written in the order they are shown. A block of a station's calendar can instead draw the captions onto the picture (ffmpeg's `subtitles` filter). `CAPTION_TRACK=0` turns the track off for the whole container.

## One guide for everything

`/xmltv.xml` is one XMLTV document covering every channel in the lineup. Jellyfin binds a guide channel to a tuner channel by trying channel id, then **number**, then name; the number is the dependable one, so every `<channel>` carries its tuner channel number as a `<display-name>`. It is built from:

| source | where it comes from |
|---|---|
| FieldStation42 | always: the `liquid_blocks` table, with a video's `.nfo` (synopsis, year, genres, episode) and its picture |
| a tuner on the Sources page | SiliconDust's guide service for that device, read with the device's own token |
| a playlist's own guide | an XMLTV address named in its header, matched by `tvg-id` |
| any other XMLTV feed | `guide.sources` in the config, by url or path (gzip handled) |

A source that fails is logged and skipped, never fatal: a network blip must not hand Jellyfin an empty guide. SiliconDust's answer is kept for three hours (and on disk, so a restart does not ask again) and left alone for half an hour after a 403, 429 or 503. `/guide_status.json` shows per-source counts and cache age, and `/xmltv.xml?refresh` forces a rebuild.

## Notes on Jellyfin

Things learned from its source that shape the tuner:

- **One Live TV view.** There is no second section or per-tuner view. The nearest thing is the *Favorite* flag (`favorite_fs42_channels`, on by default), which Jellyfin turns into a filter. `group-title` in a playlist is never read.
- **Two HDHomeRun tuners can collide.** Jellyfin builds a channel's id as `"hdhr_" + GuideNumber`, with nothing naming the device, and silently drops a channel whose id already exists. With a second device, keep its numbers apart from ours (an antenna's 20.x against our 1 to 4 is fine).
- **The model number is `HDHR4-2US` and must not contain "hdtc".** Jellyfin infers hardware transcoding from the model string and would offer profiles (`?transcode=<profile>`) that this service does not implement.
- **It speaks only HTTP.** Jellyfin falls back to HDHomeRun's native UDP protocol only for a lineup URL that starts `hdhomerun`; ours are `http://`.
- **The device id is its identity.** Changing `DeviceID` makes Jellyfin treat it as a new tuner and lose the guide mapping.
- **Jellyfin's apps do not list an embedded caption track on live TV**, so for Jellyfin the captions have to be drawn on the picture.

## Known rough edges

- **Seek artefacts.** Joining mid-file seeks to the nearest keyframe, so the first frame or two can show a corrupted macroblock if the source has sparse keyframes.
- **No direct play.** Everything is transcoded; that is the price of a continuous stream assembled from different files. It costs CPU permanently.
- **Player-side features don't come along.** The on-screen display, PPV and parental-control gates live in FieldStation42's player, not in the schedule.
- **Safari.** The web player uses [mpegts.js](https://github.com/xqq/mpegts.js) over MSE, which Safari does not support for raw MPEG-TS; iPhone, iPad and Apple TV need a different client (VLC, Jellyfin's app).

## Files

| | |
|---|---|
| `tuner.py` | the HTTP service: HDHomeRun endpoints, XMLTV, encoder pool, fan-out, the dashboard's API |
| `resolver.py` | schedule to `(file, seek, remaining)`; no dependencies beyond the standard library |
| `guide.py` | merges FieldStation42, SiliconDust, playlists and XMLTV feeds into one document |
| `guide_channel.py` | the scrolling TV guide channel, drawn here and streamed like any other |
| `iptv.py`, `upstream.py` | external sources (tuners, playlists, streams) and the real HDHomeRun behind them |
| `ccinject.py`, `captions.py` | CEA-608 captions in the stream; subtitle files for the web player |
| `stations.py`, `stationform.py`, `stationsapi.py`, `schedules.py`, `keeper.py`, `legacy.py` | stations, their calendar and schedule, kept running a few days ahead |
| `files.py`, `library.py`, `titles.py`, `art.py`, `channel_logo.py` | lists and the file browser, names, pictures and logos |
| `settings.py`, `auth.py`, `diag.py`, `prefetch.py`, `breaksnap.py`, `breakstore.py` | settings, login, status page, read-ahead, commercial-break points |
| `static/` | the dashboard and web player (plain files, no build step; `mpegts.min.js` is vendored by the Dockerfile) |
| `test_*.py` | one per module; CI runs each directly (`python3 test_resolver.py`) |
