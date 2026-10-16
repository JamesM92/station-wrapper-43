# Changelog

All notable changes. Format after [Keep a Changelog](https://keepachangelog.com); versions after [Semantic Versioning](https://semver.org).
Work in progress is kept under *Unreleased* and moves to a version heading when that version is released.

## [Unreleased]

Work towards 0.8.0. The upgrade notes come first.

### Upgrade notes
- **The services no longer run as root.** The container starts as root only to give `/config` and the catalog to `PUID`:`PGID` and
  then drops to that user. Unset, they are the owner of your config folder (so run `setup-dirs.sh` as the account that should own your
  files), else 1000:1000. The first start after upgrading gives whatever a root run left in those two folders (the schedule
  database, the TubeArchivist view) to that user, once; the log says so. The account must be able to read your media: on a NAS share
  that restricts who can, set `PUID` and `PGID` to an account that can. `PUID=0` keeps root (not recommended), and starting the
  container with `user:` in compose never uses root at all.
- **Episodes of a show now play in natural order** (`Episode 2` before `Episode 10`), where FieldStation42 sorted by plain file
  path. A show that is part way through keeps its place across the change.
- **New hidden folders in your lists and catalog**, made and kept by the wrapper and safe to ignore: `.loop.<show>` and
  `.new.<show>.<n>` (premiere nights), `.generated-cards` and `.cards.gen.<channel>` (drawn mid cards), and `catalog/.prefetch`
  (the read-ahead server's disk tier, at most 30 GB).
- **New settings** (all optional): `WARM_START_SECONDS`, `PREFETCH_STREAM`, `PREFETCH_DISK_GB`, `PREFETCH_DISK_DIR`; a *Playback* panel
  on the Stations page for continuous timestamps. Compose files that name the variables pass blanks through safely.

### Added
**Scheduling**
- **A slot can be on several days.** Drag a slot sideways onto another day (a 15:00-16:00 slot on Monday, dragged to Tuesday, is on both
  at 15:00-16:00), or draw across several days on empty space: it is one slot, a piece on each day at the same time with the same
  lists, mid cards and captions. Change any of them, or resize it, once and every day follows. *Remove this day*, *Delete all days* and
  *Split into separate days* undo it; Alt while dragging moves a slot instead. The buttons *Weekdays*, *Weekend* and *All days* now
  link the slot to those days (they used to make separate copies).
- **Premiere nights.** Tick *New episodes premiere on* the nights of a linked slot and choose where a show's new episodes start (a
  season, a folder or an episode). Each premiere night plays the next new episode; every other airing of the show plays the older
  episodes in order and loops, including the ones already premiered. With no new episode left the night carries on with the loop, and
  with nothing premiered yet the show plays from its first episode. It is worked out from the date, so a rebuilt schedule agrees with
  itself. The slot editor previews the next six nights with the episode each airs (its title when the library has one) and how many new
  episodes are left.

**Channels and cards**
- **Mid cards drawn for a channel.** On the station form, with *Default mid cards* on, *Also draw cards for this channel* adds two cards
  made from the channel's name (2 seconds, 1280x720: *Station Wrapper 43* and the name as 3D letters, one rigid piece that swings about
  40 degrees with a slight nod, and *We'll be right back* or *We're back with more* along the bottom) to the start and end lists. Each
  break picks from your lists and these like any other card; your lists are never changed, so a list shared by several channels stays
  as it is. They are drawn in the background on save, on a rename and when the style changes, in any of the guide's looks (Modern blue,
  Midnight, Classic cable, Emerald, Sunset); a failure only shows a note on the station, which then plays its lists as before.
- **The channel guide merges clips too small to read.** A run of programmes each too narrow to show a name (under about 8 minutes
  at the usual three hours across, down to slivers of a second; a 15 minute programme keeps its own cell) is one cell that names its
  clips in turn, says how many it holds, and marks the one on air with ▶, first in each round.
- **Even volume.** Shows and commercials are brought to one loudness. Each video is measured once in the background (EBU R128) and
  a fixed gain with a limiter is applied when it plays. The target is set on the Stations page (-23 LUFS by default); a station can
  switch it off, and `LOUDNESS=0` turns it off for the container.
- **Channel logos on the dashboard**: a station's own (the one set on the station, else one drawn from its name) and an external
  source's channel's, when its source or its guide has one.

**Playback**
- **Warm start.** Each piece of a channel is started before it is due, so its picture comes out on time instead of after the file has
  been opened and the encoder started: a part of a show that resumes after a break 4 seconds early (`WARM_START_SECONDS`, 0 switches it
  off), so its open and seek happen while the break is still playing; a commercial or a card 0.8 seconds early. The log marks them
  *[warm start]*.
- **Big files on a share are read through a read-ahead server.** A file of 256 MB or more on a network share is played through a small
  local server that holds a head start in the RAM disk (then `catalog/.prefetch`) and drops what has been played. The head is
  `bytes still to play x (1 - share speed / the file's own rate)`, never under about a minute: a few GB for a long film on a slow
  share, falling to nothing by the end. A read waits for its bytes; pulling ahead takes at most half of the share's measured speed;
  the join due soonest is served first. `PREFETCH_STREAM=0` turns it off, and any error plays the file directly as before.
- **A record of the joins between pieces.** The dashboard's *Joins* card and *Recent joins* table show how late each piece's picture
  came against the schedule, whether it was warm-started or read ahead, and a median, 90th percentile and worst over the last hour. A
  tune-in is counted apart. A join slower than a second is written to the log.
- **A switch for continuous timestamps** (*Playback* on the Stations page, off by default, used from the next piece) to compare in
  the Joins card before it ever becomes the default; the `CONTINUOUS_TS` variable still decides when it is set.

### Changed
- **The signal files of the container's own services** are in a private folder (`/run/fs42`, 0700) instead of fixed names in `/tmp`.
- **`PUID` / `PGID` are explained where a new user looks**: the README, the QUICKSTART, `.env.example` and the end of `setup-dirs.sh`.
- **The dashboard is lighter and its pictures cheaper.** A channel nobody is watching shows *idle* and no video is opened for it; once
  somebody watches it, its picture refreshes every 20 seconds, taken from the few seconds of output the channel already keeps. The
  TV guide channel's tile is drawn here without an encoder. A channel that is off air, has a file that is gone or fell back to the
  test pattern shows it with its reason in the corner (*NO PROGRAMMING*, *NO FILE: name*, *BAD FILE*, *NO VIDEOS IN: list*...).
  A picture stays on screen until its replacement has loaded (no flicker). The Library section moved to the Lists & files page (the
  counts, the warnings beside their list, and **Rescan**).
- **A channel keeps running for a while after the last viewer leaves.** A station of ours finishes what is queued (the programme and the
  next whole break with its mid cards) and stops at the start of the break after that, or after 15 minutes (`IDLE_TAIL_MINUTES`, `0`
  for the old 10 seconds); a source converted here stays up for 5 minutes (`external_linger_seconds`). Nothing new is read ahead for
  nobody, a returning viewer cancels the wind-down, and when every encoder is busy the channel idle the longest gives its encoder
  to the one being tuned to (never one with viewers, never the TV guide channel).
- **An external source's channels follow how they are played**: a converted one is treated like a station of ours (idle until
  watched, then a picture from the output the container already keeps, so the source is not asked again); one passed on untouched
  shows *passing on*; a guide-only tuner has no tiles.

### Fixed
- **A moment left at the end of a piece is no longer played again as a piece of its own.** A piece is delivered slightly ahead of its
  clock, so it ends a little before its schedule does; the channel then found the last fraction of a second of the same entry still
  due and started a whole new encode for it (repeating that tail, costing another start-up, and throwing away the next piece when it
  was already started and waiting). It now goes straight on to the next entry. Found by measuring the joins on a demo channel: the
  Joins card had shown "60 s" and "182 s late" for these tails.
- **Every finding of GitHub's code scanning is fixed in the code** (117, none dismissed), and no `# nosec` is left in our code: paths
  are joined and checked to stay inside their folder in a form the scanner follows; the login page's "go back to" address must stay on
  this server; caption text is cleaned until no tag is left; ffmpeg and ffprobe run from literal lists; every address the tuner, the
  sync service and the channel tools open goes through one opener that speaks http and https only; config files are written `0644`
  instead of `0666`; the catalog queries have no built text; and the dashboard's bind address can be narrowed with `FS42_BIND`. A guide
  channel's music must now be a folder or file *inside* the media. GitHub Actions are on checkout v7, setup-python v7 and codeql-action v4.
- **An antenna tuner's logos** were lost in the guide in converted or passed-on mode; they are now kept on the channel in `/xmltv.xml`.
- **A link to a video that is gone no longer stops a catalog rebuild.** It used to fail with `No such file or directory: .../star.mkv`
  after FieldStation42 had already deleted the station's schedule. Such a link is now left out of the scan (the console log names it
  once) and the rebuild goes on with the rest.

## [0.7.0] - 2026-10-11

The first public release.

### Added
- **Captions on the IPTV stream.** A video's captions (a `.srt`/`.vtt`/`.ass` file beside it, or a text track inside it) in the language set on the Stations page (English by default) go out in the stream as a CEA-608 caption track, which players such as VLC and many TVs list under their captions button; a station can turn that off. A block of a station's calendar can instead draw them on the picture (*Draw the captions on the picture while this plays*), always visible, for the shows that need them (a Japanese show, not an English one). Jellyfin's own apps do not list an embedded caption track on live TV, so for Jellyfin use the picture option.
- **Getting started.** A dashboard card that tracks the first steps (media, a list, a station, a build, Jellyfin) and a **Make me a
  channel now** button that makes a working channel from the biggest list or the first series in the media; station **templates**
  (movie channel, one list all day, a cartoon channel, a mainstream channel, a shuffled loop); **Duplicate** a station; a plain-words
  summary under the station form and a warning before leaving with unsaved changes.
- **One browser for choosing what plays.** Lists are folders like any other: browse Lists and Media down to a series, a season,
  a folder, one video or a YouTube playlist, in a slot, a commercials block, a mid card choice, the form's defaults and a repeating
  station. Several sources behind one choice are put together as one (a hidden, kept-up-to-date list).
- **Breaks.** Default commercials and default mid cards are opt in, and a commercials block or cards given to a time of day, a
  video slot or a show opt those hours in; separate start-of-break and end-of-break cards; a calendar **Mid cards** tab; with no
  breaks, time slots are soft (the next video comes from the slot the clock is in). Checked against the real engine
  (`scripts/e2e-breaks.py`).
- **A YouTube channel's kinds.** All standard videos, all shorts or all streams in one choice (followed as new seasons appear);
  playlists are sources like series; the playlists folder comes first, as `_Playlists`.
- **Sources page.** Other HDHomeRun tuners, playlists (with their own guide, `url-tvg`) and cameras, played through the container;
  a tuner can be **guide only**; the old single `passthrough` setting is moved onto the page.
- **Status page and diagnostics.** What is wrong first, each with a button to the page that fixes it; a media speed test; a
  diagnostics download with the recent messages and no passwords; `/lineup.m3u` for TVHeadend.
- **The guide says as much as a tuner's own.** Synopsis, aired date, kinds, season and episode, a picture per series or movie (the
  poster, the cover embedded in the video, or a frame), the categories Jellyfin sorts by, and a logo for each channel (your own or
  drawn from its name). Off-air time is a gap, not a programme.
- **A test pattern says why**, in its bottom right corner: no file, bad file, no commercial, no bump, queueing, or off air.
- **Short videos share a time slot**, and a station can **leave out** that behaviour.
- **TV guide channel** drawn live, with the show's name and the episode's name apart, scrolling long titles, and music.
- **Security checks in CI:** static analysis, a dependency audit of the pinned `requirements.txt`, a secret scan and a Trivy scan, on
  every push and weekly. `SECURITY.md`, `NOTICE`, and this changelog.

### Changed
- The image installs pinned Python dependencies from `requirements.txt`.
- New installs put the Channel Guide on channel 1 and the Test Pattern on 2 (Jellyfin ignores channel 0).
- A scheduled slot whose lists have no videos yet plays the test pattern instead of failing the build.
- Rebuilds keep the channel on air with the old schedule; a failed build says why and can be resumed.
- Find-everything-first checks on the Stations page are cached and no longer touch every link of a list.

### Fixed
- **A channel on a missing file could not be watched.** A quiet picture (the test pattern shown for a missing or unreadable file) is written in thousands of tiny pieces; the replay a new viewer is given overflowed its queue, so every viewer after the first was dropped at once and saw the stream end. The replay is now handed over in blocks.
- **A channel can't be taken off by an error any more.** Anything that went wrong while a channel was choosing or starting its next piece used to end the channel's thread, and the viewer saw the stream end and reconnect. It is now logged with its traceback, shown on the channel as *CHANNEL ERROR: <what>* for ten seconds, and the channel carries on. `CAPTION_TRACK=0` switches the caption track off for the whole container.
- **A test pattern always says why.** The schedule's own off-air video (an hour no block covers, or a block whose lists have no videos yet) now carries its reason in the corner: *NO VIDEOS IN: <lists>* or *OFF AIR: NOTHING SCHEDULED*. Before, only a block named Offair had one.
- **The antenna's guide asks SiliconDust far less.** Its listings are kept (3 hours, also on disk so a restart does not ask again), a refusal (403, 429 or 503) is backed off for 30 minutes while the last listings keep being served, and it asks 3 channels at a time instead of 8. Before, every guide build and every station build asked for dozens of pages, which could get the address rate limited.
- **Black dashboard picture.** The dashboard thumbnail of a channel that has just started a programme with a black fade-in now looks a few
  seconds further in for a real frame instead of showing black.
- A station built a minute ago missing from the TV guide channel until the guide's next hourly rebuild (it is made again when a build finishes).
- A rebuild no longer stops at a scheduled block whose video left the catalog (`'NoneType' object has no attribute 'path'`).
- The guide channel never loading when a programme was a minute or two long.
- Jellyfin showing a tuner with no channels when `advertise_url` was never set; HEAD requests answered.
- The Sources page showing a source as read in January 1970 (its time was read as milliseconds), and the guide channel failing to draw a programme only a few pixels wide on an older Pillow.
- Elements with the `hidden` attribute staying visible when a class gave them a display.

### Removed
- The DVR recorder and its Newznab indexer (recording is left to Jellyfin's DVR, NextPVR and other dedicated programs).
- The single `passthrough` device's `direct` mode (clients always play through the container).
- Making lists from chosen TubeArchivist playlists on the converter panel (a playlist is chosen where it is used).
