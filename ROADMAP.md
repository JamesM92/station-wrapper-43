# Station Wrapper 43: things to do next

Written 2026-10-08, at a stopping point. Everything below is **not done**. What is built and pushed is described in
`README.md` and `docs/REFERENCE.md` (user-facing) and the source (layout and decisions). Newest first within each part. "Tried" means it
was run against the real thing; "untried" means only unit tests or reading the code.

## 0. The principle (decided 2026-10-08, by the owner)

**Everything is funneled through the container**: one authoritative lineup and guide, merged into our TV guide, and Jellyfin
(and TVHeadend) use the container as their single source. Done: a Sources-page tuner's channels go through the container and its
real listings are merged onto them in `/xmltv.xml` (tried on the FLEX 4K). To finish the principle:
- (Done 2026-10-10: the single `passthrough` setting is moved onto the Sources page at start and removed from the config file; `direct` mode is retired. The recorder and its Newznab indexer were removed on 2026-10-10: recording is left to Jellyfin's DVR, NextPVR and the like.)
- A source's channel numbers that clash are moved; keep Jellyfin's channel mapping stable across that (it keys on the number).
- Cameras have only a "Live" programme in the guide. (Done 2026-10-10: a playlist's own XMLTV, named by `url-tvg` in its header, is merged onto its channels by `tvg-id`; tested with a synthetic feed only.)
- Then item 1 below makes TVHeadend read the same single source.

### Where the antenna channels' guide comes from (decided 2026-10-08: SiliconDust by default)

Now: SiliconDust's guide service (`https://api.hdhomerun.com/api/guide.php?DeviceAuth=<token from the tuner's discover.json>`).
Tried on the FLEX 4K: it answers with only the device token (no account or login; a bare call gets 403), about 4 to 5 hours
in the first reply and 48 hours after paging. The terms are **not checked** (whether it is free with the device for good,
and what the DVR subscription changes), and the old address `my.hdhomerun.com` already went 403 once, so do not rely on it forever.
To do:
- **A guide fallback/alternative setting**: let a tuner (or the whole lineup) take its listings from an XMLTV source instead of, or as
  well as, SiliconDust (Schedules Direct, a free XMLTV grabber, a URL). The guide code already merges XMLTV sources (`guide.sources`,
  type `xmltv`, with `number_map` to line the ids up); it needs a settings page and per-tuner choice, and the same retagging onto
  the lineup's channel numbers that the SiliconDust listings get.
- **Over-the-air guide (PSIP/EIT)**: the broadcast itself carries a guide (titles and times for the next hours to days, for each
  sub-channel). The HDHomeRun's HTTP interface does not expose it (`lineup.json` has only number, name, codecs and signal), so reading
  it means tuning each RF channel and parsing the EIT tables from the transport stream: TVHeadend does this natively; here it would
  need a small parser (or `dvbtee`/similar) run when a channel is idle. Untried. Check what the FLEX 4K's ATSC 3.0 firmware
  offers (different signalling from ATSC 1.0; the channels seen were MPEG-2/AC3, i.e. ATSC 1.0).

Ways to get the over-the-air guide, easiest first (none tried; from general knowledge, check before relying on it):
1. **The HDHomeRun itself**: besides the per-channel stream we use, the device can be told (with SiliconDust's `hdhomerun_config`, filter
   `0x0000-0x1FFF`) to send the whole transport stream of a tuned channel, which includes the guide tables (PID 0x1FFB). A small job
   could tune one channel at a time when the tuner is idle and parse the EIT/ETT tables (TSDuck's `tstables`, or `dvbtee`). No extra hardware.
2. **A USB ATSC tuner** (the kind Linux and TVHeadend already support): TVHeadend reads the guide itself.
3. **An SDR** (software-defined radio): possible but the hardest. ATSC 1.0 (8VSB) is 6 MHz wide, wider than the common RTL-SDR dongles
   can capture (about 2.4 to 3.2 MHz), so it would need a wider SDR (Airspy, HackRF, SDRplay) and a demodulator (GNU Radio's ATSC blocks),
   then the same EIT parsing. ATSC 3.0 (OFDM) is a different demodulator again. Not worth it while an HDHomeRun is already in hand.

## 1. Feed TVHeadend (asked for, not started)

Goal: TVHeadend's guide should include the HDHomeRun's channels as well as the FieldStation42 ones.

What exists to build on:
- `/lineup.json` (merged: our channels + the passthrough device's + the Sources-page channels), `/xmltv.xml` (one guide
  for every channel in the lineup; channel ids are `fs42.<number>`; the antenna's listings come from SiliconDust when a
  `passthrough` device is set, or per source for the Sources page), `/auto/v<number>` (MPEG-TS, the stream).
- (Done 2026-10-10: `GET /lineup.m3u` exists, with `tvg-id` equal to the XMLTV channel id. Steps 2 to 5 below are still untried against a real TVHeadend.)

Plan:
1. Add `GET /lineup.m3u`: `#EXTINF:-1 tvg-id="<xmltv channel id>" tvg-name="..." tvg-logo="..." group-title="...",Name` then
   `http://<advertise_url>/auto/v<n>`. The `tvg-id` MUST equal the `<channel id>` in `/xmltv.xml` (`fs42.5`, `fs42.20.1`)
   or TVHeadend will not map the guide to the channel. Number channels by `tvg-chno`. Test with a real TVHeadend.
2. In TVHeadend: Configuration > DVB Inputs > Networks > add **IPTV Automatic Network**, URL = the M3U; Muxes/Services
   then appear; map services to channels (it can do this itself, with "Map all services").
3. Guide: Configuration > Channel/EPG > EPG Grabber Modules. TVHeadend needs an XMLTV *grabber* (an executable that prints
   XMLTV): a one-line script such as `curl -s http://<host>:5004/xmltv.xml` placed where TVHeadend finds `tv_grab_*`
   (or the `xmltv` socket interface), then Channels > map each channel's "EPG source" to the XMLTV id. Document the exact
   script and where it goes (it depends on how TVHeadend is installed: package, Docker image, LinuxServer.io).
4. Decide the antenna path. Two choices: (a) give TVHeadend the HDHomeRun directly (it supports HDHomeRun as a native tuner
   and reads the over-the-air guide itself) and give it only the FieldStation42 channels from us; or (b) let the antenna
   come through us (`passthrough` or a Sources-page tuner, mode `proxy`) so everything is in one lineup and one guide.
   (b) needs the convert mode or TVHeadend's own transcoding if clients can't play MPEG-2. Write down which was chosen and why.
5. `advertise_url` must be an address TVHeadend can reach (not `localhost`).

Open questions: does the FLEX 4K's ATSC 3.0 firmware change the stream (it reported MPEG-2/AC3 here, so ATSC 1.0 channels)?
Does TVHeadend want `tvg-chno` or the channel's name as the key? Both need trying.

## 2. Playback: the lag between clips

- `CONTINUOUS_TS=1` removed the freezes in a test (26.6 s frozen over 70 s down to 0) but is **off by default** and
  untried on real transitions. Try it on the real stations, then make it the default.
- Start the next clip's ffmpeg a few seconds before the current one ends, so a slow seek on the share is spent before the cut.
- Log the real gap at every transition (the tuner has the times) so there are numbers from the real setup.
- The media share reads at about 5.6 MB/s (tried: `dd` and `ffprobe` timings). That alone makes seeks slow. Check the CIFS
  mount options (`vers=`, `cache=`, `rsize`) and the NAS; read-ahead (`prefetch.py`) helps but cannot make up for it.

## 3. Breaks and the engine

- **Break snapping** (`breaksnap.py`) and **`.edl` files** are built and tested on a synthetic clip, **untried on real episodes**.
  Run **Update videos** on one station with "Better break points" ticked, then look at `config/break-store.json`
  (before/after marks) and a day of schedule.
- `breakstore.py` is the wrapper's own record of breaks, but FieldStation42 still builds the schedule from the marks written
  into its `chapter_points` table. The next step is a planner of our own that reads the store directly and builds plans
  (the plan format `resolver.py` reads is the seam), opt-in per station, FieldStation42 kept as the fallback. Comskip
  (black frames, silence, logos) could fill the store for files with no marks at all.
- **Short videos share a slot** (`fieldstation42/console_launch.py`) wraps one FieldStation42 method at run time. Tried on the
  real engine. If FieldStation42's code moves, the console starts unchanged and the log says so; the weekly
  `upstream-compat` workflow checks the method's signature. Watch for it after every `scripts/sync-fs42.sh`.
- Episodes in a sequence are ordered by plain file path (FieldStation42). `Episode 10` sorts before `Episode 2`, and so does
  a `Season 10` folder. Zero-padded names are fine. Fix idea: pad the numbers in the link names that show lists make
  (changes some displayed names). Waiting for a decision.
- Interrupted rebuild after a reboot: **Resume the rebuild** is a button. Auto-resume was left out on purpose (a rebuild that
  keeps failing would restart on every boot); could be added for interruptions only.

## 4. Sources page (external IPTV)

Built and tried against the real HDHomeRun FLEX 4K (`192.168.1.20`) and local streams. Not done:
- **ONVIF camera source type**: give the camera's address, user and password, ask it for its stream address (ONVIF
  `GetProfiles` / `GetStreamUri`, WS-Security), so the RTSP path need not be found by hand. Auto-discovery will not work from
  Docker's bridge network (multicast). RTSP itself is **untried** (no camera was available): only HTTP streams were tested.
- (Done 2026-10-08: a tuner source's real SiliconDust listings are merged onto its channels in `/xmltv.xml`.)
- (Done 2026-10-10: the passthrough setting is shown on the Sources page with an import button.)
- No PTZ, motion events or two-way audio.
- A channel's thumbnail on the dashboard is "live stream" (no still frame is taken, to avoid starting an encoder).

## 5. The guide and names

- Guide channel: scrolling names, show name big, episode name small after the time, two laps per vertical scroll. Rendered
  and checked on frames; **not watched on a live stream**.
- Names come from the `.nfo` beside each video (`titles.py`): show or YouTube channel as the name, the episode or video
  title as the episode. Turn off with "Names in the guide" on the Stations page. A TV episode whose `.nfo` has no
  `<showtitle>` is shown by its episode title. Season/episode numbers (`S01E05`) are not shown; could be added.
- Blocks of a schedule being kept during a rebuild get names too (fixed), but only through the plan's first feature file.
- If YouTube names still look like file names after updating: run the diagnostic in the 2026-10-08 chat history (checks that
  the `.nfo` link sits beside the video inside the list); the likely cause is a list that has the video but not its `.nfo`.

## 6. Smaller items

- **IPTV captions** (shelved by the owner): needs the subtitles put into the stream; see the memory note.
- **Rename the `bump` folder** to something like `midcards`: the screens already say "channel mid cards". The folder,
  the `bump_dir` setting and the `bump` content type keep their names (FieldStation42 uses them; existing installs have the
  folder). Renaming means moving files on existing installs.
- Station form for loop/guide stations does not use the new "pack short videos" and "better break points" options (standard stations only).
- `scripts/preflight.sh` fails in the local sandbox at the Docker build's `apt-get` step (network); CI's image build is the real check.
- Tests that start a real tuner give it up to 90 seconds; a busy runner once overran 30.
- The first catalog build over the slow share took about 50 minutes for 483 videos (length pass) and again for chapter marks.
  TA_NFOWriter 0.5.0 (16 workers) should be faster on the YouTube conversion; untimed.

## 7. How the work is released

Everything goes to `dev` (CI builds the `:dev` image on each push). Nothing has been merged to `main`, tagged or released;
that happens only when asked, through the dev-to-main pull request. Version numbers are untouched.
