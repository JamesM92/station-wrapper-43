"""One definition of "episode order" for the whole wrapper: numbers compare as numbers, so Episode 2 comes before Episode 10 and
Season 2 before Season 10. FieldStation42 orders a sequence by plain file path (Episode 10 first); fieldstation42/console_launch.py
puts this order in its place, and premieres.py uses the same one, so the new episodes and the loop agree.
"""
import re

_PARTS = re.compile(r"(\d+)")


def natural_key(path):
    """A sort key for a path: each folder and the file name compared in turn, digit runs as numbers, letters without case."""
    out = []
    for part in str(path).replace("\\", "/").split("/"):
        key = []
        for piece in _PARTS.split(part):
            if piece.isdigit():
                key.append((0, int(piece), len(piece)))        # a number (longer zero padding sorts after, so ties are stable)
            elif piece:
                key.append((1, piece.casefold(), 0))
        out.append(tuple(key))
    return tuple(out)
