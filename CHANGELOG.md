# Changelog

All notable changes. Format after [Keep a Changelog](https://keepachangelog.com); versions after [Semantic Versioning](https://semver.org).
Work in progress is kept under *Unreleased* and moves to a version heading when that version is released.

## [Unreleased]

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
- **Screenshots and a demo library.** `scripts/fetch-demo-media.py` fetches public-domain demo videos (marked so by the Internet Archive) and `scripts/make-screenshots.py` makes the pictures in `docs/images` from a real container.
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
