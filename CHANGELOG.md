# Changelog

All notable changes. Format after [Keep a Changelog](https://keepachangelog.com); versions after [Semantic Versioning](https://semver.org).
Work in progress is kept under *Unreleased* and moves to a version heading when that version is released.

## [Unreleased]

### Changed
- **The services no longer run as root.** The container starts as root only to give `/config` and the catalog to `PUID`:`PGID` (unset: the owner of your config folder, else 1000:1000) and drops to that user before anything runs; the console, dashboard, sync and TubeArchivist jobs, `fs42-channels` and everything they write are that user's. The first start gives what an earlier run as root left in those two folders to that user, once. Start the container with `user:` to never be root at all. `PUID=0` keeps root. With `cap_drop: ALL`, add back `CHOWN`, `SETUID`, `SETGID`.
- **The services' signal files are in a private folder** (`/run/fs42`, 0700) instead of fixed names in `/tmp`.
- **The dashboard's pictures are cheaper and say why.** A channel nobody is watching shows **idle** and no video is opened for it
  (a snapshot reads the video, which on a network share is real work); once somebody watches it, its picture refreshes every
  20 seconds. The TV guide channel shows its own guide, drawn here and refreshed every 10 seconds, without an encoder running.
  A channel that is off air, has nothing scheduled, has a file that is gone, or fell back to the test pattern shows the test
  pattern with its reason in the corner (*NO PROGRAMMING*, *NO FILE: name*, *BAD FILE*, *NO VIDEOS IN: list*...).
- **A channel keeps running for a while after the last viewer leaves, and the dashboard's pictures follow it.** A tile shows
  *idle* only when its encoder is not running, and its picture is taken from the few seconds of output the channel already keeps,
  so no file is opened for it. A source converted here stays up for 5 minutes after the last viewer (`external_linger_seconds`).
  A station of ours finishes what is queued (the programme and the next whole commercial break with its mid cards) and stops at
  the start of the break after that, or after 15 minutes (`IDLE_TAIL_MINUTES`, `0` for the old 10 seconds), whichever is first.
  Nothing new is read ahead for nobody, and a viewer who comes back cancels the wind-down. When every encoder is busy and somebody
  tunes to another channel, the channel that has been idle the longest gives up its encoder at once; a channel with viewers is
  never taken from, and neither is the TV guide channel.
- **An external source's channels on the dashboard follow how they are played.** A converted one is treated like a station of our own:
  *idle* until somebody watches it, then a picture that refreshes every 20 seconds like a station's (taken from the few seconds of its own output this container already keeps, so the source is not asked again). One passed on as it is says *passing on*, since nothing here watches it. A
  guide-only tuner has no channels of ours, so it has no tiles.
- **No flicker when the dashboard updates.** A picture stays on screen until its replacement has loaded (the guide channel's
  picture used to blank for a moment each time it was drawn again).
- **The dashboard is lighter.** The Library section (the lists, tag by tag) is gone from the dashboard. The counts, the dead-link and
  duplicate warnings (beside the list they are in) and the **Rescan** button are on the Lists & files page, above the lists.

### Added
- **A switch for continuous timestamps.** The Stations page has a *Playback* panel with **Continuous timestamps (experimental)**, off
  unless turned on (the `CONTINUOUS_TS` variable still decides when it is set). It is used from the next piece, so you can compare the
  dashboard's *Joins* card with it off and on before it ever becomes the default.
- **A preview of the premiere nights.** In the slot editor, under *The new episodes start at*, the next six premiere nights are listed with
  the episode each airs (its title when the library has one, else its file) and a line saying how many new episodes are left and when the
  last one airs, worked out from the schedule as it is being edited, by the same date logic that builds the config.
- **A record of the joins between pieces.** The dashboard has a *Joins* card and a *Recent joins* table: for each cut between pieces,
  how late its picture came against when the schedule had it due, whether it was warm-started or read ahead, and (as a median,
  90th percentile and worst over the last hour) how the warm ones compare with the plain ones. A tune-in is counted apart, by its time to
  the first picture. A join slower than a second is also written to the log. It is kept in memory (the last 200).
- **Big files on a share are read through a read-ahead server.** A file of 256 MB or more on a network share is played through a
  small server inside the container that holds a head start in the RAM disk (then a local disk folder, `catalog/.prefetch`, up to
  `PREFETCH_DISK_GB`, 30 by default) and drops what has been played. How much it holds ahead is worked out from what the share is
  doing now: `bytes still to play x (1 - share speed / the file's own rate)`, never under about a minute, so it starts at a few GB for
  a long film on a slow share and falls to nothing by the end. A read waits for its bytes, so playing never gets ahead of what has
  arrived; pulling ahead takes at most half of the share's measured speed; the join due soonest is served first, and the
  dashboard's *Big files* card shows what each file has and is short by. `PREFETCH_STREAM=0` turns it off; any error plays the file
  directly as before.
- **Warm start.** Each piece of a channel is now started before it is due, so its picture comes out when it should instead of
  after the file has been opened and the encoder has started. A part of a show that resumes after a break is started 4 seconds
  early (`WARM_START_SECONDS`, 0 switches it off) so that the open and the seek happen while the commercials or cards are still
  playing; a piece that starts at the beginning of its file (a commercial, a card) is started 0.8 seconds early, the time an
  ffmpeg takes to put out its first picture. The log marks them *[warm start]*.
- **Mid cards drawn for a channel.** On the station form, with *Default mid cards* on, *Also draw cards for this channel* adds two
  cards made from the channel's name (2 seconds, 1280x720: *Station Wrapper 43* and the name in 3D letters, one rigid piece, that swing about 40 degrees back
  and forth with a slight nod, with *We'll be right back* or *We're back with more* flat along the bottom) to the start and end lists. Each break picks
  from your lists and these like any other card, and your lists are never changed (a list shared by several channels stays as it is):
  the clips sit together in one hidden folder, and each channel's own hidden break folder links to its two clips and to your chosen
  lists. They are drawn in the background on save and again on a rename, and a failure only shows a note on the station: it plays its
  lists as before. The cards can be drawn in any of the guide's looks (Modern blue, Midnight, Classic cable, Emerald, Sunset), chosen
  beside the option, so a channel's cards and its guide can match; changing it draws them again.
- **The channel guide merges clips too small to read.** A run of programmes that are each too narrow to show a name (a feed of short
  clips) is drawn as one cell over the whole run, which names its clips in turn (a long name moves along the cell while it is up)
  and says how many clips it holds and the time it covers. While one of them is on air its name is marked with ▶ and comes
  first each round.
- **Premiere nights.** Mark one block of a show's slot (on the Videos tab: select it and tick *New episodes premiere in this slot*,
  say Friday's 8 pm) and choose where the show's new episodes start (a season, a folder or an episode). Each time that slot comes
  round it plays the next new episode; the show's other airings play the older episodes in order and loop, now including the ones
  already premiered. When there is no new episode left the premiere night carries on with the loop, and with nothing premiered
  yet the show simply plays from its first episode. It is worked out from the date, so a rebuilt schedule agrees with itself:
  the config names a hidden folder per premiere (`.new.<show>.<n>`) and one for the loop (`.loop.<show>`), which the keeper
  makes and adds to the catalog as the nights come into range.
- **A slot can be on several days.** On the calendar's Videos tab, drag a slot sideways onto another day (a 15:00-16:00 slot
  on Monday, dragged to Tuesday, is on both at 15:00-16:00), or draw across several days on empty space, and it is one slot: a
  piece on each day at the same time, with the same lists, mid cards and captions. Change any of them, or resize it, once and every day
  follows; *Remove this day* takes one out, *Delete all days* clears it, *Split into separate days* makes the pieces ordinary slots
  again. The buttons *Weekdays*, *Weekend* and *All days* now link the slot to those days (they used to make separate copies).
  For a premiere, the slot's editor lists its days and you tick the night(s) there, and choose where the new episodes start, once. Hold Alt while dragging to move a slot instead of putting it on the next day. It is a calendar convenience: each day is
  still built on its own.
- **Even volume.** Shows and commercials are brought to one loudness. Each video is measured once in the background (EBU R128) and a
  fixed gain, with a limiter, is applied when it plays; a video not measured yet plays as it is. The target is set on the Stations
  page (-23 LUFS by default), a station can switch it off, and `LOUDNESS=0` turns it off for the container.
- **Channel logos on the dashboard.** Each channel's card shows its logo in the top left of its picture: a station's own (the one set
  on the station, else one drawn from its name), and an external source's channel's, when its source or its guide has one.

### Fixed
- **Every finding of GitHub's code scanning is fixed in the code** (117, none dismissed). A page's file or list name is joined to its
  folder and checked to stay inside it in one function per module, in a form the scanner follows; the login page's "go back to"
  address is resolved against this server and used only if it stays on it; the caption text is cleaned until no tag is left; the
  commands for ffmpeg and ffprobe are literal lists with the video or address as one argument; the redirect header can hold no
  line break; and the captions route answers 404 to a path that is not one of its two shapes. A guide channel's music must now be a
  folder or file *inside* the media, not the media folder itself.
- **No `# nosec` is left in our code.** Every address the tuner, the sync service and the channel tools open now goes through one
  opener that speaks http and https only (a `file:` or `ftp:` address, or a redirect to one, is refused); settings, schedules,
  the follow list and the handover file are written `0644` (owned by the config folder's owner) instead of `0666`; the two
  catalog queries have no built text; the signal files sit in the system temp folder rather than a fixed `/tmp` path; and the
  dashboard's bind address can be narrowed with `FS42_BIND` (default every interface, as a container needs).
- **GitHub Actions** are on checkout v7, setup-python v7 and codeql-action v4.
- **An antenna tuner's logos in the guide.** A tuner added in converted or passed-on mode lost the logos that came with its listings;
  they are now kept on the channel in `/xmltv.xml`, so Jellyfin shows them in those modes too (guide only already did).
- **A link to a video that is gone no longer stops a catalog rebuild.** A list that held a link whose video had been moved or deleted
  made the rebuild fail with `No such file or directory: .../star.mkv` after FieldStation42 had already deleted the station's
  schedule. Such a link is now left out of the scan (the console log names it once) and the rebuild goes on with the rest.

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
