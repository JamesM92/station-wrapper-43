"""Temporary: which path guards the scanner recognises. Not part of the project."""
import os
import pathlib
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

ROOT = "/srv/media"


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        rel = unquote(urlparse(self.path).path)
        self.a(rel)
        self.b(rel)
        self.c(rel)
        self.d(rel)
        self.e(rel)
        self.f(rel)
        self.g(rel)
        self.h(rel)
        self.i(rel)

    def a(self, rel):                      # A: normpath + startswith, raise inline
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT + os.sep):
            raise ValueError("outside")
        return open(full, "rb").read()                                    # SINK_A

    def b(self, rel):                      # B: compound condition
        full = os.path.normpath(os.path.join(ROOT, rel))
        if full != ROOT and not full.startswith(ROOT + os.sep):
            raise ValueError("outside")
        return open(full, "rb").read()                                    # SINK_B

    def c(self, rel):                      # C: realpath + startswith
        full = os.path.realpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT + os.sep):
            raise ValueError("outside")
        return open(full, "rb").read()                                    # SINK_C

    def d(self, rel):                      # D: the guard in a helper that returns the path
        return open(_within(ROOT, rel), "rb").read()                      # SINK_D

    def e(self, rel):                      # E: commonpath
        full = os.path.normpath(os.path.join(ROOT, rel))
        if os.path.commonpath([full, ROOT]) != ROOT:
            raise ValueError("outside")
        return open(full, "rb").read()                                    # SINK_E

    def f(self, rel):                      # F: pathlib resolve + is_relative_to
        full = pathlib.Path(ROOT, rel).resolve()
        if not full.is_relative_to(ROOT):
            raise ValueError("outside")
        return open(full, "rb").read()                                    # SINK_F

    def g(self, rel):                      # G: a whitelist of characters for a single name
        if not re.fullmatch(r"[A-Za-z0-9 ._()-]+", rel) or rel.startswith("."):
            raise ValueError("bad name")
        return open(os.path.join(ROOT, rel), "rb").read()                 # SINK_G

    def h(self, rel):                      # H: the name must be an entry that listdir shows (lookup, not use)
        names = os.listdir(ROOT)
        if rel not in names:
            raise ValueError("no such")
        return open(os.path.join(ROOT, rel), "rb").read()                 # SINK_H

    def i(self, rel):                      # I: os.path.basename, one level only
        name = os.path.basename(rel)
        return open(os.path.join(ROOT, name), "rb").read()                # SINK_I


def _within(base, rel):
    full = os.path.normpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        raise ValueError("outside")
    return full
