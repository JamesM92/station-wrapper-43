import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webget


class OnlyHttp(unittest.TestCase):
    def test_a_file_address_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            with self.assertRaises(urllib.error.URLError):
                webget.open_url("file://" + f.name)

    def test_other_schemes_are_refused(self):
        for url in ("ftp://example.invalid/x", "data:text/plain,hi"):
            with self.assertRaises(urllib.error.URLError):
                webget.open_url(url)


if __name__ == "__main__":
    unittest.main()
