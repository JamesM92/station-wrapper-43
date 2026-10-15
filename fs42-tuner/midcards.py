"""Generated mid cards: "We'll be right back" and "We're back with more", drawn for one channel.

Each card is 2 seconds, 1280x720, 30 fps, silent, like the splash card: "Station Wrapper 43" and the channel's name as 3D
text that turns a little back and forth, and the caption flat along the bottom. They are an extra choice in a station's
break (the station's own lists stay as they are and are never changed):

    <lists>/.generated-cards/<key> - start.mp4          the clips, one pair per channel, all in this one folder
    <lists>/.generated-cards/<key> - end.mp4
    <lists>/.generated-cards/manifest.json              which name and design each pair was drawn for
    <lists>/.cards.gen.<key>/pre/                       the station's break folder (FieldStation42 reads `pre` and `post`):
    <lists>/.cards.gen.<key>/post/                        a link to the chosen list and a link to the one clip

The 3D is drawn with Pillow only: the text is a flat picture turned about a vertical axis and projected with a
perspective, with a stack of darker copies behind it for its depth.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError:                     # drawing says so when asked; the rest of the tuner does not need it
    Image = ImageDraw = ImageFont = ImageFilter = None

GEN_DIR = ".generated-cards"
MANIFEST = "manifest.json"
VERSION = 3                             # bump when the design changes: the cards are drawn again
SIZE = (1280, 720)
FPS = 30
SECONDS = 2                             # short: a break's own time is for the commercials
SWING = 40.0                            # degrees either side, about the vertical axis
NOD = 9.0                               # and a small nod about the horizontal axis, a quarter of a turn behind the swing
DEPTH = 56                              # pixels of thickness at the front
SLICES = 14
BRAND = "Station Wrapper 43"
CAPTIONS = {"start": "We'll be right back", "end": "We're back with more"}
FONT_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
TOP, BOTTOM = (6, 16, 46), (14, 40, 100)      # the guide's dark blue gradient


class CardError(Exception):
    """The cards could not be drawn. The text is for the person who saved the station."""


# ---- names ------------------------------------------------------------------------------------------------------
def key(name):
    """A file-system safe name for a channel, readable and unique to the exact name."""
    plain = re.sub(r"[^A-Za-z0-9 _-]+", "_", str(name)).strip(" ._-")[:40] or "channel"
    return f"{plain}-{hashlib.sha1(str(name).encode('utf-8', 'replace'), usedforsecurity=False).hexdigest()[:6]}"


def folder_name(name):
    """The station's break folder (the config's bump_dir): hidden, one per channel."""
    return f".cards.gen.{key(name)}"


def clip_name(name, kind):
    return f"{key(name)} - {kind}.mp4"


def _gen_dir(lists_dir):
    return os.path.join(lists_dir, GEN_DIR)


def _manifest(lists_dir):
    try:
        with open(os.path.join(_gen_dir(lists_dir), MANIFEST)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(lists_dir, data):
    os.makedirs(_gen_dir(lists_dir), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_gen_dir(lists_dir), prefix=".manifest-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o644)  # the owner is the lists folder's: written by the user the services run as
    os.replace(tmp, os.path.join(_gen_dir(lists_dir), MANIFEST))


def current(lists_dir, name):
    """Are this channel's two clips there, drawn for this exact name and this design?"""
    got = _manifest(lists_dir).get(key(name)) or {}
    return (got.get("name") == name and got.get("version") == VERSION
            and all(os.path.isfile(os.path.join(_gen_dir(lists_dir), clip_name(name, k))) for k in ("start", "end")))


# ---- the drawing --------------------------------------------------------------------------------------------------
def _font(size):
    for p in FONT_BOLD:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    raise CardError("the font the cards are drawn with (DejaVu Sans Bold) is not installed")


def _fit(text, width, start, floor):
    """The biggest font size at or under `start` that sets `text` in `width` pixels (down to `floor`; shorter after that)."""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    size = start
    while size > floor and probe.textlength(text, font=_font(size)) > width:
        size -= 4
    font = _font(size)
    if probe.textlength(text, font=font) > width:                # still too wide at the smallest size: shorten it
        while len(text) > 1 and probe.textlength(text + "…", font=font) > width:
            text = text[:-1]
        text += "…"
    return text, font


def _plane(name):
    """The flat 3D text as one RGBA picture: the brand small, the channel's name large, centred."""
    w = 1000
    brand, f_brand = _fit(BRAND, w, 64, 40)
    title, f_title = _fit(name, w, 150, 44)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    hb = int(f_brand.size * 1.25)
    ht = int(f_title.size * 1.25)
    gap = 18
    img = Image.new("RGBA", (w + 40, hb + gap + ht + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for text, font, y in ((brand, f_brand, 4), (title, f_title, hb + gap)):
        x = (img.width - int(probe.textlength(text, font=font))) // 2
        d.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return img


def _solve(a, b):
    """Solve the small linear system a x = b (Gaussian elimination): the 8 numbers of a perspective transform."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def _coeffs(dst, w, h):
    """Perspective coefficients for Image.transform: the picture's corners (0,0) (w,0) (w,h) (0,h) land on `dst`."""
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return _solve(a, b)


def _corners(w, h, theta, phi, z, cx, cy, focal=1500.0):
    """Where the corners of a w x h plane land on screen when the whole body is turned `theta` radians about the vertical
    axis and nodded `phi` about the horizontal one through its middle, with its depth position `z` (negative is behind the
    face), seen with a perspective. Every corner goes through the same turn: the text is one rigid piece."""
    out = []
    ct, st, cp, sp = math.cos(theta), math.sin(theta), math.cos(phi), math.sin(phi)
    for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        X = x * ct + z * st                          # turn about the vertical axis
        Z = -x * st + z * ct
        Y = y * cp - Z * sp                          # then nod about the horizontal one
        Z = y * sp + Z * cp
        s = focal / (focal + Z)
        out.append((cx + X * s, cy + Y * s))
    return out


def _tint(img, rgb):
    """The same shape in one flat colour."""
    flat = Image.new("RGBA", img.size, rgb + (255,))
    flat.putalpha(img.getchannel("A"))
    return flat


def _face(plane):
    """The front of the letters: white at the top fading to a light blue, inside a dark blue edge."""
    w, h = plane.size
    grad = Image.new("RGB", (1, h))
    grad.putdata([(255, 255, 255) if y < h * 0.25 else (int(255 - 70 * (y - h * 0.25) / (h * 0.75)), int(255 - 40 * (y - h * 0.25) / (h * 0.75)), 255) for y in range(h)])
    face = grad.resize((w, h)).convert("RGBA")
    face.putalpha(plane.getchannel("A"))
    edge = _tint(plane.filter(ImageFilter.MaxFilter(7)), (8, 28, 80))
    edge.alpha_composite(face)
    return edge


def _background():
    col = Image.new("RGB", (1, SIZE[1]))
    col.putdata([tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * y / (SIZE[1] - 1)) for i in range(3)) for y in range(SIZE[1])])
    return col.resize(SIZE)


def _text_frames(name):
    """Yield the picture of the turned text for each frame (a full-frame RGBA and where the text is). The two lines are one
    plane, so they turn as one rigid body: a swing back and forth over the clip, easing at each end so it loops without a
    jump, with a small nod a quarter of a swing behind it. Its thickness is a stack of copies of the letters' own shape."""
    plane = _plane(name)
    pad = 8
    w, h = plane.size
    face = _face(plane)
    cx, cy = SIZE[0] / 2, SIZE[1] * 0.40
    total = FPS * SECONDS
    layers = []
    for s in range(SLICES, 0, -1):
        depth = s / SLICES
        lit = 1 - 0.65 * depth                                         # lighter at the front edge, darker toward the back
        layers.append((-DEPTH * depth, _tint(plane, (int(40 * lit), int(110 * lit), int(235 * lit)))))
    for i in range(total):
        phase = 2 * math.pi * i / total
        theta = math.radians(SWING) * math.sin(phase)
        phi = math.radians(NOD) * math.sin(phase + math.pi / 2)
        dims = (w + 2 * pad, h + 2 * pad)
        quads = [(_corners(*dims, theta, phi, z, cx, cy), img) for z, img in layers]
        front = _corners(*dims, theta, phi, 0, cx, cy)
        xs = [x for q, _ in quads for x, _y in q] + [x for x, _y in front]
        ys = [y for q, _ in quads for _x, y in q] + [y for _x, y in front]
        x0, y0 = max(int(min(xs)) - 2, 0), max(int(min(ys)) - 2, 0)
        x1, y1 = min(int(max(xs)) + 3, SIZE[0]), min(int(max(ys)) + 3, SIZE[1])
        box = (x1 - x0, y1 - y0)
        canvas = Image.new("RGBA", box, (0, 0, 0, 0))

        def put(img, corners, resample):
            padded = Image.new("RGBA", dims, (0, 0, 0, 0))
            padded.paste(img, (pad, pad))
            shifted = [(x - x0, y - y0) for x, y in corners]
            canvas.alpha_composite(padded.transform(box, Image.PERSPECTIVE, _coeffs(shifted, *dims), resample))
        for q, img in quads:
            put(img, q, Image.BILINEAR)
        put(face, front, Image.BILINEAR)
        yield canvas, (x0, y0)


def _caption(text):
    """The flat caption along the bottom, with a soft shadow, as one RGBA picture of the whole frame."""
    font = _font(54)
    layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x = (SIZE[0] - int(d.textlength(text, font=font))) // 2
    y = int(SIZE[1] * 0.80)
    shadow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x + 3, y + 4), text, font=font, fill=(0, 0, 0, 200))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(4)))
    ImageDraw.Draw(layer).text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return layer


def _encoder(path):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{SIZE[0]}x{SIZE[1]}",
           "-r", str(FPS), "-i", "pipe:0", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", str(SECONDS), "-shortest",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k",
           "-movflags", "+faststart", path]
    try:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise CardError(f"ffmpeg could not be started: {e}")


def draw(lists_dir, name, log=print):
    """Draw both clips for a channel into the shared folder (one pass over the 3D frames, two encoders), and note what they
    were drawn for. CardError on failure (nothing half written is left behind)."""
    if Image is None:
        raise CardError("the generated cards need Pillow (python3 -m pip install pillow)")
    if not str(name).strip():
        raise CardError("the channel has no name to draw")
    out = _gen_dir(lists_dir)
    os.makedirs(out, exist_ok=True)
    bg = _background().convert("RGBA")
    jobs = []
    try:
        for kind, text in CAPTIONS.items():
            final = os.path.join(out, clip_name(name, kind))
            tmp = os.path.join(out, f".{clip_name(name, kind)}.part.mp4")
            base = bg.copy()
            base.alpha_composite(_caption(text))
            jobs.append({"final": final, "tmp": tmp, "base": base, "proc": _encoder(tmp)})
        for layer, at in _text_frames(str(name)):
            for j in jobs:
                frame = j["base"].copy()
                # the caption is under the text where they meet (they do not, but keep the order the picture is read in)
                frame.alpha_composite(layer, at)
                j["proc"].stdin.write(frame.convert("RGB").tobytes())
        for j in jobs:
            j["proc"].stdin.close()
            err = j["proc"].stderr.read().decode("utf-8", "replace")
            j["proc"].stderr.close()
            if j["proc"].wait(timeout=120) != 0:
                raise CardError("ffmpeg could not write the card: " + (err.strip().splitlines() or ["no reason given"])[-1])
        for j in jobs:
            os.replace(j["tmp"], j["final"])
    except (BrokenPipeError, subprocess.TimeoutExpired):
        raise CardError("ffmpeg stopped while the card was being written")
    finally:
        for j in jobs:
            if j["proc"].poll() is None:
                j["proc"].kill()
            if os.path.exists(j["tmp"]):
                os.unlink(j["tmp"])
    data = _manifest(lists_dir)
    data[key(name)] = {"name": name, "version": VERSION}
    _save_manifest(lists_dir, data)
    log(f"[cards] drew the mid cards for '{name}'")


# ---- the station's break folder -----------------------------------------------------------------------------------
def link(lists_dir, name, start_list, end_list):
    """The station's break folder: `pre` and `post` each hold a link to the chosen list (if it is there) and a link to this
    channel's own clip. Only links are made or replaced; nothing in the lists themselves is touched. Returns the folder name."""
    folder = os.path.join(lists_dir, folder_name(name))
    for sub, chosen, kind in (("pre", start_list, "start"), ("post", end_list, "end")):
        d = os.path.join(folder, sub)
        os.makedirs(d, exist_ok=True)
        wanted = {}
        if chosen and os.path.isdir(os.path.join(lists_dir, chosen)) and not chosen.startswith("."):
            wanted["list"] = os.path.join("..", "..", chosen)
        clip = clip_name(name, kind)
        if os.path.isfile(os.path.join(_gen_dir(lists_dir), clip)):
            wanted[clip] = os.path.join("..", "..", GEN_DIR, clip)
        for entry in os.listdir(d):                           # what this folder used to link to and no longer should
            p = os.path.join(d, entry)
            if os.path.islink(p) and entry not in wanted:
                os.unlink(p)
        for entry, target in wanted.items():
            p = os.path.join(d, entry)
            if os.path.islink(p) and os.readlink(p) == target:
                continue
            if os.path.lexists(p):
                os.unlink(p)
            os.symlink(target, p)
    return folder_name(name)


def forget(lists_dir, name):
    """Take a channel's clips, links and note away (the station was deleted or renamed). The lists are never touched."""
    folder = os.path.join(lists_dir, folder_name(name))
    for sub in ("pre", "post"):
        d = os.path.join(folder, sub)
        if os.path.isdir(d):
            for entry in os.listdir(d):
                p = os.path.join(d, entry)
                if os.path.islink(p):
                    os.unlink(p)
            os.rmdir(d)
    try:
        os.rmdir(folder)
    except OSError:
        pass
    for kind in CAPTIONS:
        try:
            os.unlink(os.path.join(_gen_dir(lists_dir), clip_name(name, kind)))
        except OSError:
            pass
    data = _manifest(lists_dir)
    if data.pop(key(name), None) is not None:
        _save_manifest(lists_dir, data)
