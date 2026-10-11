"""Dead links in the lists are removed before a catalog is built (console_client.prune_dead_links)."""
import os
import shutil
import tempfile
import unittest

import console_client as cc


class DeadLinksTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.media = os.path.join(self.root, "media")
        self.tags = os.path.join(self.root, "by-genre")
        os.makedirs(os.path.join(self.media, "anime"))
        os.makedirs(os.path.join(self.tags, "Action & Adventure", "anime"))
        self.good = self.link("good.mkv", True)

    def tearDown(self):
        shutil.rmtree(self.root)

    def link(self, name, real):
        target = os.path.join(self.media, "anime", name)
        if real:
            open(target, "w").close()
        p = os.path.join(self.tags, "Action & Adventure", "anime", name)
        os.symlink(target, p)
        return p

    def test_a_link_whose_video_is_gone_is_removed_and_the_rest_stay(self):
        dead = self.link("mush.mkv", False)
        self.assertEqual(cc.prune_dead_links(self.tags, self.media), (1, None))
        self.assertFalse(os.path.lexists(dead))
        self.assertTrue(os.path.islink(self.good))

    def test_nothing_to_do_is_quiet(self):
        self.assertEqual(cc.prune_dead_links(self.tags, self.media), (0, None))

    def test_an_unmounted_share_does_not_empty_the_lists(self):
        dead = self.link("mush.mkv", False)
        shutil.rmtree(os.path.join(self.media, "anime"))                     # the share is away: every link is dead and media is empty
        removed, why = cc.prune_dead_links(self.tags, self.media)
        self.assertEqual(removed, 0)
        self.assertIn("unmounted", why)
        self.assertTrue(os.path.islink(dead) and os.path.islink(self.good))

    def test_most_links_dead_at_once_is_not_pruned(self):
        for i in range(30):
            self.link(f"gone{i}.mkv", False)
        removed, why = cc.prune_dead_links(self.tags, self.media)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(why)

    def test_a_dead_folder_link_goes_too(self):
        os.symlink(os.path.join(self.media, "nowhere"), os.path.join(self.tags, "Action & Adventure", "folder-link"))
        self.assertEqual(cc.prune_dead_links(self.tags, self.media)[0], 1)


if __name__ == "__main__":
    unittest.main()
