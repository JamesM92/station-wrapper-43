"""A video's picture: the one beside it, the cover embedded in it, a frame of it, or the show's poster.
Run: python3 test_art.py   (needs ffmpeg)"""
import os
import shutil
import subprocess
import tempfile
import unittest

import titles

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class ArtTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["FS42_ART_CACHE"] = os.path.join(self.d, "cache")
        import importlib
        import art
        self.art = importlib.reload(art)
        self.plain = os.path.join(self.d, "plain.mp4")
        ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x120:d=3:r=10", "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", self.plain)
        cover = os.path.join(self.d, "embed-source.jpg")
        ffmpeg("-f", "lavfi", "-i", "color=c=green:s=320x180:d=1", "-frames:v", "1", cover)
        self.covered = os.path.join(self.d, "covered.mp4")
        ffmpeg("-i", self.plain, "-i", cover, "-map", "0", "-map", "1", "-c", "copy", "-disposition:v:1", "attached_pic", self.covered)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_the_cover_embedded_in_a_video_is_used(self):
        got = self.art.get(self.covered)
        self.assertIsNotNone(got)
        path, ctype = got
        self.assertEqual(ctype, "image/jpeg")
        self.assertTrue(path.startswith(os.environ["FS42_ART_CACHE"]))                     # kept in our own folder, not beside the video
        self.assertFalse(os.path.exists(os.path.splitext(self.covered)[0] + "-thumb.jpg"))
        self.assertEqual(self.art.get(self.covered)[0], path)                              # made once

    def test_a_video_with_no_cover_gives_a_frame_of_itself(self):
        got = self.art.get(self.plain)
        self.assertIsNotNone(got)
        self.assertGreater(os.path.getsize(got[0]), 200)

    def test_every_episode_of_a_show_shares_one_picture_and_a_movie_has_its_own(self):
        import art as artmod
        nfo = "<episodedetails><showtitle>Mystery Theater</showtitle><title>%s</title></episodedetails>"
        eps = []
        for i, src in enumerate((self.covered, self.plain), 1):
            p = os.path.join(self.d, f"ep{i}.mp4")
            shutil.copy(src, p)
            open(os.path.join(self.d, f"ep{i}.nfo"), "w").write(nfo % f"Episode {i}")
            eps.append(p)
        first = self.art.get(eps[0])[0]
        second = self.art.get(eps[1])[0]
        self.assertEqual(first, second)                                    # one picture for the series, whichever episode asks
        movie = os.path.join(self.d, "film.mp4")
        shutil.copy(self.covered, movie)
        open(os.path.join(self.d, "film.nfo"), "w").write("<movie><title>Film</title></movie>")
        self.assertNotEqual(self.art.get(movie)[0], first)
        self.assertEqual(len(os.listdir(os.environ["FS42_ART_CACHE"])), 2)           # two pictures made, not three

    def test_a_shows_poster_comes_before_anything_taken_from_a_video(self):
        show = os.path.join(self.d, "Show")
        os.makedirs(show)
        vid = os.path.join(show, "e1.mp4")
        shutil.copy(self.covered, vid)
        open(os.path.join(show, "e1.nfo"), "w").write("<episodedetails><showtitle>Show</showtitle><title>One</title></episodedetails>")
        open(os.path.join(show, "poster.jpg"), "wb").write(b"\xff\xd8 poster")
        self.assertEqual(self.art.get(vid)[0], os.path.join(show, "poster.jpg"))
        self.assertFalse(os.path.isdir(os.environ["FS42_ART_CACHE"]) and os.listdir(os.environ["FS42_ART_CACHE"]))

    def test_a_picture_beside_the_video_comes_before_the_embedded_one(self):
        side = os.path.splitext(self.covered)[0] + "-thumb.jpg"
        open(side, "wb").write(b"\xff\xd8 mine")
        self.assertEqual(self.art.get(self.covered)[0], side)

    def test_the_shows_poster_is_the_last_resort_for_a_file_that_cannot_be_read(self):
        show = os.path.join(self.d, "Show")
        os.makedirs(show)
        open(os.path.join(show, "poster.jpg"), "wb").write(b"\xff\xd8 poster")
        broken = os.path.join(show, "broken.mp4")
        open(broken, "wb").write(b"not a video")
        self.assertEqual(self.art.get(broken)[0], os.path.join(show, "poster.jpg"))


if __name__ == "__main__":
    unittest.main()
