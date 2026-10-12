"""Temporary: more guards. Not part of the project."""
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, quote, parse_qsl, urlencode

ROOT = "/srv/media"
PAGES = ("/index.html", "/files.html", "/stations.html")
PAGESET = frozenset(PAGES)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        rel = unquote(u.path)
        self.h1(self.path)
        self.h2(self.path)
        self.h3(self.path)
        self.h4(u.path, u.query)
        self.h5(u.path, u.query)
        self.p1(rel)
        self.p2(rel)
        self.p3(rel)
        self.p4(rel)

    def h1(self, v):                       # H1: strip CR and LF
        v = v.replace("\r", "").replace("\n", "")
        self.send_header("Location", "/login?next=" + v)                           # HDR_1

    def h2(self, v):                       # H2: percent-encode
        self.send_header("Location", "/login?next=" + quote(v, safe=""))           # HDR_2

    def h3(self, v):                       # H3: refuse a line break
        if "\r" in v or "\n" in v:
            raise ValueError("bad")
        self.send_header("Location", "/login?next=" + v)                           # HDR_3

    def h4(self, path, query):             # H4: the page from a constant list, the query rebuilt and encoded
        if path not in PAGES:
            raise ValueError("page")
        self.send_header("Location", "/login?next=" + path + "?" + urlencode(parse_qsl(query)))   # HDR_4

    def h5(self, path, query):             # H5: same, with a frozenset constant
        if path not in PAGESET:
            raise ValueError("page")
        self.send_header("Location", "/login?next=" + path)                        # HDR_5

    def p1(self, rel):                     # P1: strict form A with a separate equality branch returning the root
        if not rel:
            return open(ROOT, "rb").read()
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT + os.sep):
            raise ValueError("outside")
        return open(full, "rb").read()                                              # PATH_1

    def p2(self, rel):                     # P2: relpath idiom
        full = os.path.normpath(os.path.join(ROOT, rel))
        if os.path.relpath(full, ROOT).startswith(".."):
            raise ValueError("outside")
        return open(full, "rb").read()                                              # PATH_2

    def p3(self, rel):                     # P3: abspath + startswith
        full = os.path.abspath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT + os.sep):
            raise ValueError("outside")
        return open(full, "rb").read()                                              # PATH_3

    def p4(self, rel):                     # P4: a name checked, then joined and re-checked (two guards)
        name = os.path.basename(rel)
        full = os.path.normpath(os.path.join(ROOT, name))
        if not full.startswith(ROOT + os.sep):
            raise ValueError("outside")
        return [f for f in os.listdir(full)]                                        # PATH_4
