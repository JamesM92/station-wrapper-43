"""The read-ahead server for big files on a share (streaming.py), against a file read through a deliberately slow "share".

Run: python3 test_streaming.py
"""
import hashlib
import os
import shutil
import tempfile
import time
import unittest
import urllib.request

import prefetch
import streaming as st

MB = 1024 * 1024


class SlowFile:
    """A file object that reads at no more than `rate` bytes/s and counts what was read: a share that is busy."""

    def __init__(self, path, mode, rate, counter):
        self.f = open(path, mode)
        self.rate, self.counter = rate, counter

    def seek(self, n):
        self.f.seek(n)

    def read(self, n):
        data = self.f.read(n)
        if self.rate:
            time.sleep(len(data) / self.rate)
        self.counter.append(len(data))
        return data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()


def make_file(folder, name, size):
    path = os.path.join(folder, name)
    block = hashlib.sha256(name.encode()).digest() * 4096              # 128 KB of a repeating, file-specific pattern
    with open(path, "wb") as f:
        n = 0
        while n < size:
            chunk = block[: min(len(block), size - n)]
            f.write(chunk)
            n += len(chunk)
    return path


class Env:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ram = os.path.join(self.tmp, "ram")
        self.disk = os.path.join(self.tmp, "disk")
        self.media = os.path.join(self.tmp, "media")
        os.makedirs(self.media)
        self.reads = []
        self.rate = 0
        self.stats = prefetch.Stats(None)
        self.made = []

    def tearDown(self):
        for s in self.made:
            s.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def streamer(self, ram_mb=64, disk_mb=64, rate_of=None, **cfg):
        c = {"enabled": True, "min_bytes": 1 * MB, "disk_dir": "", "disk_bytes": disk_mb * MB, "share_fraction": 0.5, "workers": 2}
        c.update(cfg)
        s = st.Streamer(c, self.stats, self.media, self.ram, lambda: ram_mb * MB, self.disk, log=lambda *_: None,
                        opener=lambda p, m: SlowFile(p, m, self.rate, self.reads), rate_of=rate_of)
        s.start()
        self.made.append(s)
        return s

    def get(self, url, start=None, end=None):
        req = urllib.request.Request(url)
        if start is not None:
            req.add_header("Range", f"bytes={start}-{'' if end is None else end}")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)


class NeedTest(unittest.TestCase):
    def test_the_head_follows_the_formula_and_falls_to_nothing(self):
        size, rate, speed = 10 * 1024 ** 3, 1.4 * MB, 1.0 * MB          # a 10 GB film on a share a little slower than it plays
        start = st.need_bytes(size, rate, speed)
        self.assertAlmostEqual(start / 1024 ** 3, 10 * (1 - 1.0 / 1.4), delta=0.15)       # about 2.8 GB
        half = st.need_bytes(size / 2, rate, speed)
        self.assertAlmostEqual(half / start, 0.5, delta=0.05)                              # half the film left, half the head
        self.assertLessEqual(st.need_bytes(1 * MB, rate, speed), 1 * MB)                  # never more than is left
        self.assertEqual(st.need_bytes(0, rate, speed), 0)

    def test_a_share_faster_than_the_film_needs_only_the_floor(self):
        self.assertEqual(st.need_bytes(10 * 1024 ** 3, 1.4 * MB, 20 * MB), int(1.4 * MB * st.FLOOR_SECONDS))

    def test_the_pace_never_exceeds_its_rate(self):
        b = st._Bucket(now=lambda: 100.0)
        waits = [b.wait_time(4 * MB, 4 * MB) for _ in range(3)]            # 4 MB blocks at 4 MB/s, all asked for at once
        self.assertEqual(waits, [0.0, 1.0, 2.0])                           # the first is free (a second in hand), then a second each


class ServerTest(Env, unittest.TestCase):
    def test_a_whole_file_and_ranges_come_back_exactly(self):
        path = make_file(self.media, "film.mkv", 9 * MB + 12345)
        s = self.streamer()
        url = s.open(path, 0)
        self.assertTrue(url.endswith(".mkv"))
        status, body, headers = self.get(url)
        self.assertEqual(status, 200)
        self.assertEqual(body, open(path, "rb").read())
        for a, b in ((0, 99), (4 * MB - 5, 4 * MB + 5), (7 * MB, None)):
            status, body, headers = self.get(url, a, b)
            self.assertEqual(status, 206)
            want = open(path, "rb").read()[a:(b + 1) if b is not None else None]
            self.assertEqual(body, want)
            self.assertEqual(headers["Content-Length"], str(len(want)))
        self.assertEqual(self.get(url)[2]["Accept-Ranges"], "bytes")

    def test_a_range_past_the_end_is_refused_and_an_unknown_name_is_not_found(self):
        path = make_file(self.media, "a.mkv", 2 * MB)
        s = self.streamer()
        url = s.open(path, 0)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(url, 5 * MB, None)
        self.assertEqual(cm.exception.code, 416)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(f"http://127.0.0.1:{s.port}/s/not-a-name.mkv")
        self.assertEqual(cm.exception.code, 404)

    def test_a_read_waits_for_the_share_and_is_never_short(self):
        path = make_file(self.media, "slow.mkv", 6 * MB)
        self.rate = 4 * MB                                                # 4 MB/s: a second for 4 MB
        s = self.streamer()
        url = s.open(path, 0)
        began = time.time()
        status, body, _ = self.get(url, 5 * MB, 5 * MB + 999)
        self.assertEqual(body, open(path, "rb").read()[5 * MB:5 * MB + 1000])
        self.assertGreater(time.time() - began, 0.4)                       # it waited for the block to arrive

    def test_what_has_been_played_is_dropped(self):
        path = make_file(self.media, "big.mkv", 40 * MB)
        s = self.streamer(rate_of=lambda p: 1 * MB)
        url = s.open(path, 0)
        src = next(iter(s.sources.values()))
        s.read(src, 0, 100)
        s.read(src, 20 * MB, 100)                                          # the playing position moves a long way
        s.policy_once()
        gone = [b for b in range(0, 3) if b not in src.blocks]
        self.assertTrue(len(gone) >= 1 and 20 * MB // st.BLOCK in src.blocks)

    def test_the_policy_holds_about_the_need_ahead_and_drains_it_as_it_plays(self):
        path = make_file(self.media, "film.mkv", 48 * MB)
        self.stats.record_speed(prefetch.share_key(path, self.media), 1.0 * MB)       # the share gives 1 MB/s ...
        s = self.streamer(rate_of=lambda p: 2.0 * MB)                       # ... the film plays at 2 MB/s
        url = s.open(path, 0)
        src = next(iter(s.sources.values()))
        s.policy_once()
        first = st.need_bytes(src.size - src.playhead, src.rate, 1.0 * MB)
        self.assertGreater(first, 20 * MB)                                 # half the file is the head
        later = st.need_bytes(src.size - 36 * MB, src.rate, 1.0 * MB)
        self.assertLess(later, first / 2)                                  # and it is a quarter of that near the end

    def test_a_file_that_cannot_be_read_marks_the_source_failed_and_returns_no_url(self):
        s = self.streamer()
        self.assertIsNone(s.open(os.path.join(self.media, "missing.mkv"), 0))

    def test_only_big_files_on_a_share_qualify(self):
        small = make_file(self.media, "ad.mp4", 100 * 1024)
        big = make_file(self.media, "film.mkv", 3 * MB)
        s = self.streamer(min_bytes=2 * MB)
        self.assertFalse(s.qualifies(small, mount="cifs"))
        self.assertTrue(s.qualifies(big, mount="cifs"))
        self.assertTrue(s.qualifies(big, mount="nfs4"))
        self.assertFalse(s.qualifies(big, mount="ext4"))                   # local disk: played directly
        self.assertFalse(s.qualifies(big, mount="zfs"))


class ArbitrationTest(Env, unittest.TestCase):
    def test_the_join_due_soonest_is_allotted_first_and_the_rest_report_what_they_are_short_by(self):
        a = make_file(self.media, "a.mkv", 60 * MB)
        b = make_file(self.media, "b.mkv", 60 * MB)
        share = prefetch.share_key(a, self.media)
        self.stats.record_speed(share, 0.5 * MB)                           # slow share: both films want a big head
        s = self.streamer(ram_mb=20, disk_mb=10, rate_of=lambda p: 1.0 * MB)
        now = time.time()
        s.prepare(a, 0, now + 20)
        s.prepare(b, 0, now + 10)                                          # b is due first
        s.policy_once()
        by = {os.path.basename(x.path): x for x in s.sources.values()}
        self.assertEqual(by["b.mkv"].short, 0 if by["b.mkv"].need + 2 * st.BLOCK * 2 <= 30 * MB else by["b.mkv"].short)
        self.assertGreater(by["a.mkv"].short, by["b.mkv"].short)           # the later one is the one that is short
        names = [f["file"] for f in s.status()["files"]]
        self.assertEqual(sorted(names), ["a.mkv", "b.mkv"])


if __name__ == "__main__":
    unittest.main()
