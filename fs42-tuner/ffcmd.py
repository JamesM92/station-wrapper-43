"""The ffmpeg command lines for one piece of a channel (a plan entry, a test pattern, a held picture), and the video settings
they share. tuner.py reads the "video" section of its config and hands it to `configure`; everything here is a function of
those settings and its arguments."""
import os
import re

import settings

W, H, FPS, GOP = 1280, 720, 30, 60
NORMALIZE = []
MUX = [
    "-f", "mpegts",
    "-mpegts_flags", "+resend_headers+initial_discontinuity",
    "-mpegts_service_id", "1", "-mpegts_pmt_start_pid", "4096", "-mpegts_start_pid", "256",
    "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
]


def configure(video):
    """Set the picture size, frame rate and encoder options from the config's "video" section."""
    global W, H, FPS, GOP, NORMALIZE
    FPS = int(video.get("fps", 30))
    W, H = int(video.get("width", 1280)), int(video.get("height", 720))
    GOP = FPS * 2                       # keyframe every 2s -- also the join latency
    NORMALIZE = [
        "-c:v", video.get("vcodec", "libx264"),
        "-preset", video.get("preset", "veryfast"),
        "-b:v", video.get("vbitrate", "4000k"),
        "-maxrate", video.get("vbitrate", "4000k"),
        "-bufsize", video.get("bufsize", "8000k"),
        "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
               f"pad={W}:{H}:-1:-1:color=black,fps={FPS},format=yuv420p",
        "-g", str(GOP), "-keyint_min", str(GOP), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", video.get("abitrate", "192k"), "-ar", "48000", "-ac", "2",
    ]


configure({})


CONT_ENV = os.environ.get("CONTINUOUS_TS", "").strip().lower()


def continuous_ts():
    """Do timestamps carry straight on across pieces (one continuous stream)? The CONTINUOUS_TS environment variable decides
    when it is set; else the dashboard's switch (settings.json), off unless turned on. Read at each piece, so a change is used
    from the next one."""
    if CONT_ENV:
        return CONT_ENV in ("1", "true", "yes", "on")
    try:
        return settings.get_playback()["continuous_ts"]
    except Exception:
        return False


def _continuous(mux):
    """The mux options without the flag that marks each new ffmpeg as a break in the stream."""
    return [("+resend_headers" if m == "+resend_headers+initial_discontinuity" else m) for m in mux]


def segment_cmd(path, seek, duration, is_stream, ts_offset=None, text=None, subs=None, ordered=False, audio=None, lead=0.0):
    """ffmpeg for one plan entry. -ss before -i so the seek is fast, -t bounds
    it to the scheduled duration so a file that runs long can't push the
    channel off its own clock. -re paces output at realtime: without it ffmpeg
    would encode a 20-minute segment as fast as the CPU allows and the fanout
    would race ahead of the wall clock it is supposed to track.

    `lead` (a warm start, see WARM_LEAD): it is started that many seconds before it is due, reading from `lead` seconds
    earlier in the file, and the first `lead` seconds of what comes out are dropped before they are encoded. The file is
    opened and seeked during that time, and the picture the viewer gets starts at `seek`, on time."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re"]
    if is_stream:
        cmd += ["-i", path]
    elif lead > 0:
        cmd += ["-ss", f"{max(seek - lead, 0):.3f}", "-i", path, "-ss", f"{lead:.3f}", "-t", f"{duration:.3f}"]
    else:
        cmd += ["-ss", f"{seek:.3f}", "-i", path, "-t", f"{duration:.3f}"]
    enc = with_subtitles(NORMALIZE, subs)
    if audio:                                    # the volume brought to the target: one fixed gain, then a limiter
        enc = enc + ["-af", audio]
    if ordered:                                  # captions are put in frame by frame: the frames must be in the order they are shown
        enc = enc + ["-bf", "0"]
    if ts_offset is not None:                    # timestamps carry on from the last segment: one continuous stream
        return with_text(cmd + enc + ["-output_ts_offset", f"{ts_offset:.3f}"] + _continuous(MUX) + ["pipe:1"], text)
    return with_text(cmd + enc + MUX + ["pipe:1"], text)


def with_subtitles(enc, subs):
    """The encoder options with `subs` (a filter from captions.burn_filter) drawn on the picture, or unchanged."""
    if not subs or "-vf" not in enc:
        return enc
    out = list(enc)
    i = out.index("-vf")
    out[i + 1] = f"{out[i + 1]},{subs}"
    return out


TEXT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _drawtext(text):
    """An ffmpeg drawtext filter that puts `text` in the bottom right corner on a dark box."""
    t = re.sub(r"[^\w .,:!?'()/#&+@-]", "", str(text))[:70]
    t = t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")
    font = f":fontfile={TEXT_FONT}" if os.path.isfile(TEXT_FONT) else ""
    return (f"drawtext=text='{t}'{font}:fontcolor=white:fontsize=h/26:box=1:boxcolor=black@0.65:boxborderw=10:"
            "x=w-tw-30:y=h-th-30")


def with_text(cmd, text):
    """The same ffmpeg command with `text` drawn in the bottom right of the picture (why a test pattern is on screen)."""
    if not text or "-vf" not in cmd:
        return cmd
    out = list(cmd)
    i = out.index("-vf")
    out[i + 1] = f"{out[i + 1]},{_drawtext(text)}"
    return out


def filler_cmd(seconds, text=None):
    return with_text(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                      "-f", "lavfi", "-i", f"smptebars=size={W}x{H}:rate={FPS}",
                      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                      "-t", f"{max(seconds, 1):.1f}"] + NORMALIZE + MUX + ["pipe:1"], text)


def still_cmd(path, seconds, text=None):
    """A picture held on screen (a be-right-back card) for `seconds`, with silence under it."""
    return with_text(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                      "-loop", "1", "-framerate", str(FPS), "-i", path,
                      "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                      "-t", f"{max(seconds, 1):.1f}"] + NORMALIZE + MUX + ["pipe:1"], text)
