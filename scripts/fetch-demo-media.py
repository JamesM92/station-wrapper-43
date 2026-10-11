#!/usr/bin/env python3
"""Fetch a small demo library of public-domain videos from the Internet Archive, for trying the wrapper and for screenshots.

    python3 scripts/fetch-demo-media.py [--dest demo-media] [--features] [--only shows,movies,commercials,cards]

It makes the folders the wrapper expects, with a .nfo beside each video (the show's name, the episode's, the synopsis), a poster
where the Archive has one, short "we'll be right back" cards made with ffmpeg, and SOURCES.md saying where each file came from.

Only items the Internet Archive itself marks as public domain (a Creative Commons Public Domain Mark or CC0 license in the
item's metadata) are taken; anything else is skipped with a message. That mark is the Archive's and the uploader's statement, not
legal advice: check an item's page (linked in SOURCES.md) before you redistribute a file. This script downloads for you and the
files are not part of this repository.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

API = "https://archive.org"
UA = {"User-Agent": "StationWrapper43-demo-fetch/1.0"}

# (identifier, title for the guide, year)
SHOWS = {
    "Fleischer Superman Cartoons": {
        "plot": "The Fleischer Studios Superman cartoons of 1941 to 1943.", "genres": ["Animation", "Action"],
        "episodes": [("superman_1941", "Superman", 1941), ("Superman_cartoons", "The Magnetic Telescope", 1942),
                     ("MaxFleischerSuperman", "Eleventh Hour", 1942)]},
    "Holiday Cartoons": {
        "plot": "Short cartoons for the season, from the 1930s and 1940s.", "genres": ["Animation", "Family"],
        "episodes": [("JackFrost_", "Jack Frost", 1934), ("SantasSuprise", "Santa's Surprise", 1947),
                     ("RudolphTheRed-nosedReindeer1948", "Rudolph the Red-Nosed Reindeer", 1948)]},
}
MOVIES = [("DuckandC1951", "Duck and Cover", 1951, ["Documentary", "Family"])]
FEATURES = [("his_girl_friday", "His Girl Friday", 1940, ["Comedy"]), ("Nosferatu1922", "Nosferatu", 1922, ["Horror"])]
COMMERCIALS = ["Wonderfu1960", "FolgersCoffe", "FolgersCoffe_2", "kodak_instamatic", "wonder_bread", "chevrolet", "amc_jeep",
               "ideal_dolls_3", "swing_wing", "ExtraEss1938", "FilmMyst1937"]
CARDS = [("We'll be right back", "brb"), ("Welcome back", "welcome-back")]

notes = []


def _open(url, timeout=60):
    """Open an address on the Internet Archive (https only: nothing else is ever fetched)."""
    if not url.startswith(API + "/"):
        raise ValueError(f"not an Internet Archive address: {url}")
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)  # nosec B310 - checked above


def get_json(url):
    with _open(url) as r:
        return json.load(r)


def clean(name):
    return re.sub(r'[\\/:*?"<>|]+', "", name).strip()


def xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def usable(ident):
    """(metadata, file name, size) for an item the Archive marks public domain, or (None, reason, 0)."""
    m = get_json(f"{API}/metadata/{urllib.parse.quote(ident)}")
    md = m.get("metadata", {})
    lic = str(md.get("licenseurl", ""))
    if "publicdomain" not in lic:
        return None, f"{ident}: not marked public domain by the Archive ({lic or 'no license'}), skipped", 0
    files = [f for f in m.get("files", []) if f.get("name", "").lower().endswith(".mp4") and f.get("format") in ("512Kb MPEG4", "h.264", "MPEG4", "h.264 IA")]
    files.sort(key=lambda f: int(f.get("size", 0) or 0))
    if not files:
        return None, f"{ident}: no mp4 to fetch, skipped", 0
    return md, files[0]["name"], int(files[0].get("size", 0) or 0)


def download(ident, name, dest, size):
    if os.path.isfile(dest) and (not size or os.path.getsize(dest) == size):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    url = f"{API}/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(name)}"
    tmp = dest + ".part"
    with _open(url, 120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(tmp, dest)


def poster(ident, dest):
    if os.path.isfile(dest):
        return
    try:
        with _open(f"{API}/services/img/{urllib.parse.quote(ident)}") as r:
            data = r.read()
        if len(data) > 500:
            with open(dest, "wb") as f:
                f.write(data)
    except Exception:
        pass


def source_line(ident, title, md, kind):
    notes.append(f"| {kind} | {title} | [{ident}]({API}/details/{ident}) | {md.get('licenseurl', '')} |")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def plot_of(md, fallback):
    d = md.get("description")
    if isinstance(d, list):
        d = " ".join(d)
    d = re.sub(r"<[^>]+>", " ", str(d or fallback))
    return re.sub(r"\s+", " ", d).strip()[:500]


def fetch_shows(dest):
    for show, info in SHOWS.items():
        folder = os.path.join(dest, "shows", clean(show))
        write(os.path.join(folder, "tvshow.nfo"), f"<tvshow><title>{xml(show)}</title><plot>{xml(info['plot'])}</plot>"
              + "".join(f"<genre>{xml(g)}</genre>" for g in info["genres"]) + "</tvshow>\n")
        for n, (ident, title, year) in enumerate(info["episodes"], 1):
            md, name, size = usable(ident)
            if md is None:
                print("  !", name)
                continue
            base = f"{clean(show)} - S01E{n:02d} - {clean(title)}"
            path = os.path.join(folder, "Season 1", base + ".mp4")
            print(f"  {base} ({size / 1e6:.0f} MB)", flush=True)
            download(ident, name, path, size)
            write(os.path.join(folder, "Season 1", base + ".nfo"),
                  f"<episodedetails><title>{xml(title)}</title><showtitle>{xml(show)}</showtitle><season>1</season><episode>{n}</episode>"
                  f"<aired>{year}-01-01</aired><plot>{xml(plot_of(md, info['plot']))}</plot>"
                  + "".join(f"<genre>{xml(g)}</genre>" for g in info["genres"]) + "</episodedetails>\n")
            if n == 1:
                poster(ident, os.path.join(folder, "poster.jpg"))
            source_line(ident, f"{show}: {title}", md, "show")


def fetch_movies(dest, items):
    for ident, title, year, genres in items:
        md, name, size = usable(ident)
        if md is None:
            print("  !", name)
            continue
        folder = os.path.join(dest, "movies", f"{clean(title)} ({year})")
        path = os.path.join(folder, f"{clean(title)} ({year}).mp4")
        print(f"  {title} ({size / 1e6:.0f} MB)", flush=True)
        download(ident, name, path, size)
        write(path[:-4] + ".nfo", f"<movie><title>{xml(title)}</title><year>{year}</year><plot>{xml(plot_of(md, title))}</plot>"
              + "".join(f"<genre>{xml(g)}</genre>" for g in genres) + "</movie>\n")
        poster(ident, os.path.join(folder, "poster.jpg"))
        source_line(ident, title, md, "movie")


def fetch_commercials(dest):
    for ident in COMMERCIALS:
        md, name, size = usable(ident)
        if md is None:
            print("  !", name)
            continue
        title = clean(str(md.get("title", ident)))
        print(f"  {title} ({size / 1e6:.1f} MB)", flush=True)
        download(ident, name, os.path.join(dest, "commercials", f"{title}.mp4"), size)
        source_line(ident, title, md, "commercial")


def make_cards(dest):
    """Short title cards, drawn here (not downloaded), for the start and end of a break."""
    out = os.path.join(dest, "cards")
    os.makedirs(out, exist_ok=True)
    for text, slug in CARDS:
        path = os.path.join(out, f"{slug}.mp4")
        if os.path.isfile(path):
            continue
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        vf = (f"drawtext=fontfile={font}:text='{text.replace(chr(39), chr(8217))}':fontcolor=white:fontsize=36:"
              "x=(w-text_w)/2:y=(h-text_h)/2")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1d3a5f:s=640x360:d=4:r=25",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", path], check=True)
    notes.append("| card | Drawn here with ffmpeg (not downloaded) | | |")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default="demo-media")
    ap.add_argument("--features", action="store_true", help="also fetch two full-length films (about 900 MB)")
    ap.add_argument("--only", default="shows,movies,commercials,cards")
    a = ap.parse_args()
    only = set(a.only.split(","))
    dest = os.path.abspath(a.dest)
    os.makedirs(dest, exist_ok=True)
    if "shows" in only:
        print("shows"); fetch_shows(dest)
    if "movies" in only:
        print("movies"); fetch_movies(dest, MOVIES + (FEATURES if a.features else []))
    if "commercials" in only:
        print("commercials"); fetch_commercials(dest)
    if "cards" in only:
        print("cards"); make_cards(dest)
    write(os.path.join(dest, "SOURCES.md"), "# Where these files came from\n\nEach was marked public domain by the Internet Archive item it "
          "came from (see the license column). That is the Archive's and the uploader's mark, not legal advice: check the item's page.\n\n"
          "| kind | title | item | license |\n| --- | --- | --- | --- |\n" + "\n".join(notes) + "\n")
    print(f"done: {dest}")


if __name__ == "__main__":
    sys.exit(main())
