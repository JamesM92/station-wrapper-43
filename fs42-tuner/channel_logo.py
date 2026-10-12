"""A channel's logo, for the guide: the picture a client (Jellyfin) shows beside the channel.

A station can name an image of its own (the form's Channel logo: a path inside your media, `wrapper_logo` in its config). With
none, one is drawn here from the channel's name and number, so every channel has something to show without setting anything up.
"""
import io
import os
import threading

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:                                   # no Pillow: no drawn logos, a named one still works
    Image = ImageDraw = ImageFont = None

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAX_BYTES = 2 * 1024 * 1024
FONT_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
SIZE = (400, 225)
_lock = threading.Lock()
_made = {}


def _font(size):
    for p in FONT_BOLD:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _colour(name):
    h = 0
    for c in str(name):
        h = (h * 31 + ord(c)) % 360
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h / 360, 0.55, 0.55)
    return int(r * 255), int(g * 255), int(b * 255)


def render(name, number):
    """A PNG (bytes) of the channel's number and name, or None when Pillow is not there. Remembered."""
    if Image is None:
        return None
    key = (str(name), str(number))
    with _lock:
        got = _made.get(key)
    if got:
        return got
    base = _colour(name)
    img = Image.new("RGB", SIZE, base)
    d = ImageDraw.Draw(img)
    for i in range(SIZE[1]):                                   # a plain darkening gradient: legible white text on any hue
        shade = 1 - 0.45 * i / SIZE[1]
        d.line([(0, i), (SIZE[0], i)], fill=tuple(int(c * shade) for c in base))
    d.text((24, 18), str(number), font=_font(64), fill=(255, 255, 255))
    words, lines, line = str(name).split(), [], ""
    f = _font(34)
    for w in words:                                            # the name, wrapped to the picture's width
        trial = f"{line} {w}".strip()
        if d.textlength(trial, font=f) <= SIZE[0] - 48 or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    y = SIZE[1] - 24 - 42 * min(len(lines), 3)
    for ln in lines[:3]:
        d.text((24, y), ln, font=f, fill=(255, 255, 255))
        y += 42
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    data = out.getvalue()
    with _lock:
        if len(_made) > 500:
            _made.clear()
        _made[key] = data
    return data


def custom(media_root, rel):
    """(bytes, content type) of the image a station names (a path inside the media), or None: not there, not an image, too big,
    or not inside the media folder."""
    if not rel or not media_root or os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return None
    ext = os.path.splitext(rel)[1].lower()
    if ext not in IMAGE_EXT:
        return None
    full = os.path.join(media_root, rel)
    real_root = os.path.realpath(media_root)
    real = os.path.realpath(full)
    if real != real_root and not real.startswith(real_root.rstrip(os.sep) + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    try:
        if os.path.getsize(real) > MAX_BYTES:
            return None
        with open(real, "rb") as f:
            return f.read(), {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    except OSError:
        return None
