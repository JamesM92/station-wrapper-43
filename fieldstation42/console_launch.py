#!/usr/bin/env python3
"""Starts FieldStation42's console (station_42.py -s) with the wrapper's one addition applied first.

FieldStation42 gives every video a whole time slot: an 11 minute video in a 30 minute slot gets the other 19 minutes
filled with commercials. This changes ONE thing, how long a video's block is (LiquidSchedule._calc_target_duration),
at run time, without touching FieldStation42's files:

    a video that fits two or more times into the slot gets a block that is the slot divided by how many fit.
    11 minutes in a 30 minute slot: 2 fit, so a 15 minute block (two videos to the half hour);
    9 minutes: 3 fit (10 minute blocks); 7 minutes: 4 (7.5); 1 minute: 30 (the clips run back to back).

A station that sets `wrapper_no_breaks` goes further: there are no breaks at all. A video's block is exactly its own
length, so the next video starts the moment it ends and the slot times are only a soft target.

The number that fit is the largest that divides the slot into whole seconds, so the blocks always add up to the slot and
the next slot starts exactly on time. Bumps around the video count as part of it. A station that sets
`wrapper_pack_short` to false keeps FieldStation42's own behaviour. If FieldStation42 is not as expected, it is
started untouched and the reason is printed.

Run from FieldStation42's folder, like station_42.py itself:   python3 console_launch.py [station_42.py arguments]
"""
import os
import runpy
import sys

MIN_CHUNK = 60                     # seconds: a block is never shorter than a minute


def chunk_minutes(duration, increment, extra=0.0):
    """The block length, in minutes, for a video of `duration` seconds (plus `extra` seconds of bumps) in a slot of
    `increment` minutes: the slot divided by how many times the video fits, or the slot when it fits once."""
    slot = int(round(increment * 60))
    required = float(duration) + float(extra)
    if slot <= 0 or required <= 0:
        return increment
    fit = min(int(slot // required), slot // MIN_CHUNK)
    for n in range(fit, 1, -1):
        if slot % n == 0:                                  # whole seconds, so the slot is filled exactly
            return slot / n / 60
    return increment


def patch(schedule_class):
    """Wrap schedule_class._calc_target_duration. Returns True when it was wrapped."""
    original = schedule_class._calc_target_duration
    if getattr(original, "_wrapper_pack", False):
        return True

    def packed(self, duration, increment=None, break_info=None, break_strategy=None):
        try:
            chosen = (break_info or {}).get("commercial_dir")
            if self.conf.get("wrapper_no_breaks") and (not chosen or chosen == self.conf.get("commercial_dir")):
                # the default commercials are off and this slot did not choose a list of its own: no break. A video's block is
                # exactly the video (and its bumps): the next one starts the moment it ends, so the time slots are a soft
                # target and the day is filled with videos, not commercials or mid cards
                required = float(duration) + sum((break_info or {}).get(k, {}).get("duration", 0) for k in ("start_bump", "end_bump")
                                                 if (break_info or {}).get(k))
                if required > 0:
                    return required
            if self.conf.get("wrapper_pack_short", True):
                inc = increment if increment is not None else self.conf["schedule_increment"]
                extra = sum((break_info or {}).get(k, {}).get("duration", 0) for k in ("start_bump", "end_bump")
                            if (break_info or {}).get(k))
                increment = chunk_minutes(duration, inc, extra)
        except Exception:                                   # never let this stop a schedule from being built
            pass
        return original(self, duration, increment, break_info, break_strategy)

    packed._wrapper_pack = True
    schedule_class._calc_target_duration = packed
    return True


def patch_mid_cards(catalog_class):
    """Wrap catalog_class.make_reel_fill so a station with `wrapper_mid_cards` false has breaks of commercials only (no start
    card and no end card; FieldStation42 always adds them unless asked not to), except a break whose time or show chose cards
    of its own. Returns True when it was wrapped."""
    original = catalog_class.make_reel_fill
    if getattr(original, "_wrapper_cards", False):
        return True

    def fill(self, when, length, use_bumpers=True, *args, **kwargs):
        try:
            if self.config.get("wrapper_mid_cards", True) is False:
                # the default cards are off: a break has none, unless the cards in use were chosen for this time or this show
                # (a different folder from the station's own, which the wrapper keeps as a name nothing else has)
                chosen = kwargs.get("bump_dir")
                if not chosen or chosen == self.config.get("bump_dir"):
                    use_bumpers = False
        except Exception:
            pass
        return original(self, when, length, use_bumpers, *args, **kwargs)

    fill._wrapper_cards = True
    catalog_class.make_reel_fill = fill
    return True


def patch_reset_sequences(manager_class):
    """FieldStation42 resets a show's place when a schedule is cleared (a rebuild), by reading `block.content.path` of every
    scheduled block that has a sequence. A block whose video is no longer in the catalog (the catalog was changed or rebuilt
    since) has no content, and that one line stopped the whole rebuild ("'NoneType' object has no attribute 'path'").
    Such a block has nothing to reset, so it is left out of the reading: the rebuild goes on. Returns True when wrapped."""
    original = manager_class.reset_sequences
    if getattr(original, "_wrapper_safe", False):
        return True

    def safe(self, station_config, *args, **kwargs):
        try:
            name = station_config["network_name"]
            blocks = self.schedules.get(name)
            if blocks:
                self.schedules[name] = [b for b in blocks if not (getattr(b, "sequence_key", None) and getattr(b, "content", None) is None)]
        except Exception:
            pass                                             # never let this stop the rebuild it is guarding
        return original(self, station_config, *args, **kwargs)

    safe._wrapper_safe = True
    manager_class.reset_sequences = safe
    return True


def main(argv):
    app = os.getcwd()
    if app not in sys.path:
        sys.path.insert(0, app)
    try:
        from fs42.liquid_schedule import LiquidSchedule
        patch(LiquidSchedule)
        print("console: short videos share a time slot (wrapper_pack_short; switch off per station); wrapper_no_breaks plays videos back to back", flush=True)
    except Exception as e:                                  # FieldStation42 changed: run it as it is
        print(f"console: could not apply the short-video packing ({e}); running FieldStation42 unchanged", flush=True)
    try:
        from fs42.liquid_manager import LiquidManager
        patch_reset_sequences(LiquidManager)
        print("console: a rebuild goes on past a scheduled block whose video is no longer in the catalog", flush=True)
    except Exception as e:
        print(f"console: could not apply the rebuild guard ({e}); a block with no video can stop a rebuild", flush=True)
    try:
        from fs42.catalog import ShowCatalog
        patch_mid_cards(ShowCatalog)
        print("console: wrapper_mid_cards false leaves out the start and end cards of a break", flush=True)
    except Exception as e:
        print(f"console: could not apply the mid card option ({e}); breaks keep their start and end cards", flush=True)
    sys.argv = ["station_42.py"] + list(argv)
    runpy.run_path(os.path.join(app, "station_42.py"), run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:] or ["-s"])
