"""Closed captions (CEA-608) put into the video of an MPEG-TS stream, so a player lists them as a caption track it can
switch on and off.

ffmpeg can carry captions that are already in a video but cannot make them from a subtitle file, so this sits between the
encoder and the viewers: it reads the transport stream, and in every video frame's access unit it adds the A/53 caption
data (an SEI message, ITU-T T.35) for that frame. The subtitles come from WebVTT (captions.py hands that over), turned into
pop-on captions of one byte pair a frame, the way a broadcaster's encoder sends them.

Frames are counted, not timed: the encoder is run without B-frames, so frame n of a segment that starts `start` seconds into
the file is at start + n / fps, and the order the frames are written is the order they are shown.
Only 608's basic Latin letters are carried (a character outside them is turned into its plain letter, or dropped).
"""
import re
import unicodedata

TS = 188
SYNC = 0x47
NULL = (0x80, 0x80)                      # a byte pair that says nothing, with its parity bit

# control codes, channel 1, field 1 (before parity)
RCL, EDM, ENM, EOC = 0x1420, 0x142C, 0x142E, 0x142F
ROWS = {12: (0x13, 0x40), 13: (0x13, 0x60), 14: (0x14, 0x40), 15: (0x14, 0x60)}      # PAC: first byte, second byte (white, column 0)
COLUMNS = 32
MAX_LINES = 4

_SIMPLE = {"*": "-", "\\": "/", "^": "", "_": "-", "`": "'", "{": "(", "}": ")", "|": "/", "~": "-", "’": "'", "‘": "'",
           "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "♪": "#", " ": " "}


def parity(b):
    b &= 0x7F
    return b | (0x80 if bin(b).count("1") % 2 == 0 else 0)


def pair(code):
    return (parity(code >> 8), parity(code & 0xFF))


def clean(text):
    """Text a 608 decoder can show: plain printable ASCII (accents dropped to their letter, anything else removed)."""
    out = []
    for ch in text:
        if ch in _SIMPLE:
            out.append(_SIMPLE[ch])
        elif " " <= ch <= "}" and ch not in "*\\^_`{|":
            out.append(ch)
        else:
            base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(base if base and base not in _SIMPLE else "")
    return "".join(out)


def parse_vtt(text):
    """[(start seconds, end seconds, text)] from WebVTT (what captions.vtt gives): tags and styling removed."""
    cues = []
    for block in re.split(r"\r?\n\r?\n+", text.replace("﻿", "")):
        lines = [l for l in block.splitlines() if l.strip()]
        for n, line in enumerate(lines):
            m = re.match(r"\s*(?:(\d+):)?(\d+):(\d+)[.,](\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)[.,](\d+)", line)
            if not m:
                continue
            g = m.groups()
            t = lambda h, mi, s, f: int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(f.ljust(3, "0")[:3]) / 1000.0
            body = " ".join(lines[n + 1:])
            body = re.sub(r"\{[^}]*\}|<[^>]*>", "", body)
            body = re.sub(r"\s+", " ", body.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")).strip()
            if body:
                cues.append((t(*g[0:4]), t(*g[4:8]), body))
            break
    cues.sort()
    return cues


def wrap(text):
    words, lines, cur = text.split(), [], ""
    for w in words:
        w = w[:COLUMNS]
        if cur and len(cur) + 1 + len(w) > COLUMNS:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[-MAX_LINES:]


def caption_pairs(text):
    """The byte pairs that load one pop-on caption and flip it onto the screen."""
    lines = wrap(clean(text))
    if not lines:
        return []
    seq = [pair(RCL), pair(RCL), pair(ENM), pair(ENM)]
    rows = [12, 13, 14, 15][-len(lines):]
    for row, line in zip(rows, lines):
        first, second = ROWS[row]
        seq += [(parity(first), parity(second))] * 2
        data = [ord(c) for c in line]
        if len(data) % 2:
            data.append(0)
        seq += [(parity(data[i]), parity(data[i + 1]) if data[i + 1] else 0x80) for i in range(0, len(data), 2)]
    seq += [pair(EOC), pair(EOC)]
    return seq


REFRESH_SECONDS = 4          # a caption still on screen is sent again this often, for a viewer who joins or switches track mid-way


def timeline(cues, fps):
    """{frame number: byte pair} for the whole file: every caption loaded just ahead of its time, sent again every few
    seconds while it is on screen (where the frames are free), and cleared at its end."""
    line, cursor, shown = {}, 0, []
    for i, (start, end, text) in enumerate(cues):
        seq = caption_pairs(text)
        if not seq:
            continue
        first = max(cursor, int(start * fps) - len(seq) + 1)
        for k, p in enumerate(seq):
            line[first + k] = p
        flip = first + len(seq)                       # the frame the caption comes up on
        cursor = flip
        stop = max(int(end * fps), cursor)
        shown.append((flip, stop, seq))
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        if nxt is None or int(nxt * fps) - len(caption_pairs(cues[i + 1][2])) > stop + 2:      # nothing follows at once: clear it
            for k, p in enumerate((pair(EDM), pair(EDM))):
                line[stop + k] = p
            cursor = max(cursor, stop + 2)
    step = max(1, int(REFRESH_SECONDS * fps))
    for flip, stop, seq in shown:                     # a second pass, so what the next caption needs is already in place
        at = flip + step
        while at + len(seq) < stop:
            if all(f not in line for f in range(at, at + len(seq))):
                for k, p in enumerate(seq):
                    line[at + k] = p
            at += step
    return line


def active_at(cues, t):
    """The text of the caption that is on the screen at `t` seconds, or None."""
    for start, end, text in cues:
        if start <= t < end:
            return text
    return None


def _escape(data):
    """Emulation prevention: no 00 00 00/01/02/03 inside a NAL unit."""
    out, zeros = bytearray(), 0
    for b in data:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def sei(byte_pair):
    """One H.264 SEI NAL unit (with its start code) carrying A/53 caption data: one field-1 pair."""
    cc = bytes([0xFC, byte_pair[0], byte_pair[1]])               # cc_valid, cc_type 0 (NTSC field 1), then the pair
    payload = b"\xB5\x00\x31GA94\x03" + bytes([0x40 | 1, 0xFF]) + cc + b"\xFF"
    return b"\x00\x00\x00\x01\x06" + _escape(bytes([4, len(payload)]) + payload + b"\x80")


def _put_sei(es, nal):
    """`es` (Annex B access unit) with `nal` placed before its first picture slice."""
    for m in re.finditer(b"\x00\x00\x01", es):
        i = m.end()
        if i < len(es) and (es[i] & 0x1F) in (1, 5):
            at = m.start()
            if at > 0 and es[at - 1] == 0:                       # a four byte start code
                at -= 1
            return es[:at] + nal + es[at:]
    return es


class Injector:
    """Run a transport stream through feed(); it comes out with captions in the video. One per segment of a channel."""

    def __init__(self, line, start, fps, pid=None, resume=None):
        """`line`: timeline(); `start`: where in the file this segment begins (seconds); `resume`: the text of a caption
        already on the screen at that moment, which is loaded again at the first frames."""
        self.line = line
        self.extra = {}
        if resume:
            for k, p in enumerate(caption_pairs(resume)):
                self.extra[k] = p
        self.first = int(round(start * fps))
        self.n = 0                       # frames seen
        self.pid = pid                   # the video PID (found from the stream when None)
        self.buf = b""
        self.pes = None                  # [first packet, [payloads]] of the access unit being collected
        self.cc = None
        self.out = []

    # -- finding the video
    def _learn(self, packet):
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if self.pid is not None or not packet[1] & 0x40:
            return
        off = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
        data = packet[off:]
        if pid == 0 and len(data) > 12:                          # PAT: remember the PMT's PID
            self.pmt = ((data[1 + 10] & 0x1F) << 8) | data[1 + 11] if len(data) > 12 else None
        elif getattr(self, "pmt", None) == pid and len(data) > 13:
            sect = data[1:]
            length = ((sect[1] & 0x0F) << 8) | sect[2]
            info = ((sect[10] & 0x0F) << 8) | sect[11]
            i, end = 12 + info, 3 + length - 4
            while i + 5 <= end:
                stype, epid = sect[i], ((sect[i + 1] & 0x1F) << 8) | sect[i + 2]
                if stype == 0x1B:
                    self.pid = epid
                    return
                i += 5 + (((sect[i + 3] & 0x0F) << 8) | sect[i + 4])

    # -- the stream
    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= TS:
            if self.buf[0] != SYNC:
                at = self.buf.find(bytes([SYNC]), 1)
                if at < 0:
                    out.append(self.buf)
                    self.buf = b""
                    break
                out.append(self.buf[:at])
                self.buf = self.buf[at:]
                continue
            packet, self.buf = self.buf[:TS], self.buf[TS:]
            self._learn(packet)
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if self.pid is None or pid != self.pid:
                out.append(packet)
                continue
            if packet[1] & 0x40:                                  # a new access unit starts: the one before is whole
                out.extend(self._finish())
                self.pes = [packet, []]
            if self.pes is None:
                out.append(packet)
                continue
            off = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
            if packet[3] & 0x10:
                self.pes[1].append(packet[off:])
        return b"".join(out)

    def flush(self):
        rest = b"".join(self._finish())
        tail, self.buf = self.buf, b""
        return rest + tail

    def _pair(self):
        p = self.extra.get(self.n) or self.line.get(self.first + self.n, NULL)
        self.n += 1
        return p

    def _finish(self):
        if self.pes is None:
            return []
        head, parts = self.pes
        self.pes = None
        pes = b"".join(parts)
        if len(pes) < 9 or pes[:3] != b"\x00\x00\x01":
            return self._repack(head, pes)
        hdr = 9 + pes[8]
        nal = sei(self._pair())
        body = _put_sei(pes[hdr:], nal)
        added = len(body) - len(pes[hdr:])
        length = (pes[4] << 8) | pes[5]
        newlen = 0 if length == 0 else min(length + added, 0xFFFF)
        return self._repack(head, pes[:4] + bytes([newlen >> 8, newlen & 0xFF]) + pes[6:hdr] + body)

    def _repack(self, head, payload):
        pid = ((head[1] & 0x1F) << 8) | head[2]
        adaptation = head[4:5 + head[4]] if head[3] & 0x20 else b""     # kept as it was: it carries the clock (PCR)
        if self.cc is None:
            self.cc = head[3] & 0x0F
        out, first = [], True
        while payload or first:
            ad = adaptation if first else b""
            room = TS - 4 - len(ad)
            chunk, payload = payload[:room], payload[room:]
            if len(chunk) < room:                                # the last packet: pad with stuffing in the adaptation field
                need = room - len(chunk)
                if ad:
                    ad = bytes([ad[0] + need]) + ad[1:] + b"\xFF" * need
                elif need == 1:
                    ad = b"\x00"
                else:
                    ad = bytes([need - 1, 0x00]) + b"\xFF" * (need - 2)
            flags = (0x40 if first else 0) | (pid >> 8) & 0x1F
            ctl = (0x20 if ad else 0) | 0x10 | self.cc
            self.cc = (self.cc + 1) & 0x0F
            out.append(bytes([SYNC, flags, pid & 0xFF, ctl]) + ad + chunk)
            first = False
        return out
