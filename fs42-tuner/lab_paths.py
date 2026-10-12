"""Temporary: which command-line guards the scanner recognises. Not part of the project."""
import os
import shlex
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, urlsplit, quote, parse_qs

SCHEMES = {"rtsp": "rtsp", "http": "http", "https": "https"}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        url = parse_qs(urlparse(self.path).query).get("u", [""])[0]
        self.j(url)
        self.k(url)
        self.l(url)
        self.m(url)
        self.o(url)
        self.p(url)

    def j(self, url):                        # J: the plain list
        return subprocess.run(["ffprobe", "-i", url], capture_output=True)                     # CMD_J

    def k(self, url):                        # K: scheme taken from a constant table, rest rebuilt
        parts = urlsplit(url)
        scheme = SCHEMES[parts.scheme]
        rebuilt = scheme + "://" + parts.netloc + parts.path
        return subprocess.run(["ffprobe", "-i", rebuilt], capture_output=True)                 # CMD_K

    def l(self, url):                        # L: the address goes to a file, the file is the argument
        fd, path = tempfile.mkstemp(suffix=".ffconcat")
        with os.fdopen(fd, "w") as f:
            f.write("ffconcat version 1.0\nfile " + quote(url, safe="") + "\n")
        return subprocess.run(["ffprobe", "-safe", "0", "-f", "concat", "-i", path], capture_output=True)   # CMD_L

    def m(self, url):                        # M: shlex.quote in a list
        return subprocess.run(["ffprobe", "-i", shlex.quote(url)], capture_output=True)       # CMD_M

    def o(self, url):                        # O: percent-encoded
        return subprocess.run(["ffprobe", "-i", quote(url, safe=":/?&=@%")], capture_output=True)   # CMD_O

    def p(self, url):                        # P: only the part after a constant scheme prefix is user data, checked against a character set
        if not url.startswith("rtsp://"):
            raise ValueError("scheme")
        tail = url[len("rtsp://"):]
        if not all(c.isalnum() or c in "-._:@/?=&%" for c in tail):
            raise ValueError("characters")
        return subprocess.run(["ffprobe", "-i", "rtsp://" + tail], capture_output=True)       # CMD_P
