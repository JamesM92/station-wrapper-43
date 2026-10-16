"""The station form <-> FieldStation42's station config.

This is the ONLY place that knows how FieldStation42 spells a station. The pages work with a small
"form" (below); everything FieldStation42-specific is here, so if the engine is ever replaced, this
module and the schedule reader are what change.

The form:
  {"name": "Cartoons", "number": 12, "kind": "standard",
   "blocks": [{"from": 0, "to": 6, "lists": ["Cartoons"]}, {"from": 6, "to": 24, "lists": ["Movies"]}],
   "increment": 30, "bump": "bump", "commercials": "commercials" or "",     # standard
   "breaks": "end",       # where commercials go: "standard" (interrupt the show), "end" (after it), "center" (one break in the middle)
   "loop": {"list": "Test Pattern", "shuffle": false}}                       # loop
A standard station plays the lists named for each stretch of the day. `blocks` is the same every day of the
week; a station whose days differ has `days` instead: {"monday": [blocks], ... "sunday": [blocks]} (all
seven). Only ONE of the two ever comes out of validate/parse: if every day is the same it is `blocks`.

parse(conf) goes the other way. A station the form cannot represent faithfully (several day templates,
overrides, sequences, marathons, other station types...) is NOT flattened: parse returns None and says
why, and the page shows it read-only with a pointer to the FieldStation42 console.
"""
import guide_channel
import midcards
import os
import copy
import json
import schedules

FARM = "by-genre"
CONTENT_DIR = f"catalog/{FARM}"
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
INCREMENTS = (5, 10, 15, 30, 60)         # 5 rounds a movie up to the next 5 minutes; a longer slot leaves more room for breaks
BREAKS = ("end", "standard", "center")
COM_OFF = ".commercials.off"             # the station's commercial folder while its default commercials are off (empty, hidden)
OFF_CARDS = ".cards.off"                  # the station's bump folder while its default mid cards are off (an empty, hidden folder)
SLOT_KEYS_OK = {"tags", "sequence"}        # a slot with anything else (marathon, bumps...) is advanced; "sequence" only as ours (SERIES)
ADVANCED_KEYS = ("date_overrides", "slot_overrides", "tag_overrides", "week_overrides", "active_rules",
                 "clip_shows", "autobump", "off_air_autobump", "meta_hints", "fallback_tag", "schedule_offset")
# what the form owns; everything else in an existing config is kept as it was
OWNED = ("network_name", "channel_number", "network_type", "content_dir", "bump_dir", "commercial_dir",
         "commercial_free", "schedule_increment", "day_templates", "shuffle_loop", "break_strategy", "wrapper_break_snap", "wrapper_pack_short", "wrapper_no_breaks", "wrapper_start_cards", "wrapper_end_cards", "wrapper_mid_cards", "wrapper_gen_cards", "wrapper_gen_style", "wrapper_show_cards", "tag_overrides", "wrapper_logo", "wrapper_caption_track", "wrapper_level_audio") + DAYS
MAX_NAME = 100
OFF_AIR_CLIP = os.environ.get("FS42_OFFAIR_CLIP", "/opt/assets/test-pattern.mp4")      # shipped in the image (assets/)


class FormError(ValueError):
    """The form is not valid. The text is for the person filling it in."""


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise FormError("the station needs a name")
    name = value.strip()
    if len(name) > MAX_NAME:
        raise FormError(f"the name is longer than {MAX_NAME} characters")
    if any(c in name for c in '/\\\0') or name.startswith("."):
        raise FormError("the name can't contain slashes or start with a dot")
    return name


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise FormError("the channel number must be a whole number")
    if value == 0:
        raise FormError("channel 0 can't be used: Jellyfin and some other IPTV clients ignore it. Choose 1 or higher")
    if not 1 <= value <= 9999:
        raise FormError("the channel number must be between 1 and 9999")
    return value


def _logo(value):
    """A channel's own logo: a path to an image inside your media, or "" for the one drawn from its name."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise FormError("the logo is a path to an image in your media")
    path = value.strip().replace("\\", "/").lstrip("/")
    if not path:
        return ""
    if ".." in path.split("/") or len(path) > 300 or "\0" in path:
        raise FormError("the logo must be a path inside your media, like logos/cartoons.png")
    if os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        raise FormError("the logo must be a .png, .jpg or .webp picture")
    return path


def _show_path(value):
    """A show or movie as the engine matches it: a list's name, or a folder inside a list ("Movies/Toy Story")."""
    if not isinstance(value, str):
        raise FormError("choose the show or movie the cards are for")
    parts = [p.strip() for p in value.strip().strip("/").split("/")]
    if not parts or any(not p or p in (".", "..") or "\\" in p or "\0" in p or len(p) > 120 for p in parts):
        raise FormError(f"'{value}' is not a list or a folder inside one")
    return "/".join(parts)


def _show_cards(rows):
    """[{"show", "start", "end"}]: mid cards of one show or movie, used instead of the station's own two lists."""
    if rows in (None, []):
        return []
    if not isinstance(rows, list) or len(rows) > 200:
        raise FormError("the cards for particular shows are not valid")
    out, seen = [], set()
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            raise FormError(f"the cards for show {i} are not valid")
        show = _show_path(r.get("show"))
        if show in seen:
            raise FormError(f"'{show}' has its cards chosen twice")
        seen.add(show)
        start = _list_name(r.get("start"))
        out.append({"show": show, "start": start, "end": _list_name(r.get("end")) if r.get("end") else start})
    return out


def _tag_overrides(rows):
    return {r["show"]: {"bump_dir": schedules.cards_folder(r["start"], r["end"])} for r in rows}


def show_cards_of(conf):
    """The wrapper's own per-show cards of a station config, or None when `tag_overrides` holds anything else (then the
    form must not hold the station: it would lose them)."""
    rows = conf.get("wrapper_show_cards")
    have = conf.get("tag_overrides")
    if not rows and not have:
        return []
    try:
        rows = _show_cards(rows)
    except FormError:
        return None
    return rows if have == _tag_overrides(rows) else None


def _list_name(value):
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value or value.startswith("."):
        raise FormError("a list is one folder name from the Lists page")
    return value.strip()


def _blocks(blocks):
    """Hour ranges that cover the whole day exactly once, in order. Returns them cleaned."""
    if not isinstance(blocks, list) or not blocks:
        raise FormError("choose what plays: add at least one time block")
    out, expect = [], 0
    for i, b in enumerate(blocks, 1):
        if not isinstance(b, dict):
            raise FormError(f"time block {i} is not valid")
        lo, hi = b.get("from"), b.get("to")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (lo, hi)):
            raise FormError(f"time block {i}: the hours must be whole numbers")
        if lo != expect:
            raise FormError(f"time block {i} must start at hour {expect}, so the day has no gaps or overlaps")
        if not lo < hi <= 24:
            raise FormError(f"time block {i}: the end must be after the start and no later than 24")
        lists = b.get("lists")
        if not isinstance(lists, list) or not lists:
            raise FormError(f"time block {i}: choose at least one list")
        clean = []
        for name in lists:
            name = _list_name(name)
            if name not in clean:
                clean.append(name)
        out.append({"from": lo, "to": hi, "lists": clean})
        expect = hi
    if expect != 24:
        raise FormError(f"the time blocks must reach the end of the day (they stop at hour {expect})")
    return out


GUIDE_KEYS = ("messages", "play_sound", "sound_to_play", "scroll_speed", "wrapper_guide")      # what a guide station owns


def _guide(g):
    """The guide channel's settings, cleaned: title, hours ahead, scroll speed, messages, music (a folder or file)."""
    if g is None:
        g = {}
    if not isinstance(g, dict):
        raise FormError("the guide settings are not valid")
    title = g.get("title", "Channel Guide")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 40:
        raise FormError("the guide needs a title of up to 40 characters")
    hours = g.get("hours", 3)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 6:
        raise FormError("the guide can show 1 to 6 hours ahead")
    speed = g.get("scroll_speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.1 <= speed <= 5:
        raise FormError("the scroll speed must be between 0.1 and 5")
    msgs = g.get("messages", [])
    if not isinstance(msgs, list) or len(msgs) > 20 or not all(isinstance(m, str) for m in msgs):
        raise FormError("the messages are a list of up to 20 lines of text")
    msgs = [m.strip() for m in msgs if m.strip()]
    if any(len(m) > 200 for m in msgs):
        raise FormError("a message can be at most 200 characters")
    music = g.get("music", "")
    if not isinstance(music, str) or "\0" in music or (music.strip() and not music.strip().startswith("/")):
        raise FormError("the music is a folder (or one file) inside your media, like /mnt/media/music")
    theme = g.get("theme", guide_channel.DEFAULT_THEME)
    if theme not in guide_channel.THEMES:
        raise FormError("the guide style must be one of: " + ", ".join(guide_channel.THEMES))
    return {"title": title.strip(), "hours": hours, "scroll_speed": float(speed), "messages": msgs, "music": music.strip(),
            "theme": theme}


def validate(form, existing_names=(), existing_numbers=None, original=None):
    """Clean a form or raise FormError. existing_*: the other stations, to refuse a clash.
    existing_numbers: {channel number: station name}. original: the name being edited, if any."""
    if not isinstance(form, dict):
        raise FormError("that form is not valid")
    name = _name(form.get("name"))
    number = _number(form.get("number"))
    clash = [n for n in existing_names if n.casefold() == name.casefold() and n != original]
    if clash:
        raise FormError(f"there is already a station called '{clash[0]}'")
    taken = (existing_numbers or {}).get(number)
    if taken is not None and taken != original:
        raise FormError(f"channel {number} is already used by '{taken}'")
    kind = form.get("kind")
    if kind == "guide":
        clean = {"name": name, "number": number, "kind": "guide", "guide": _guide(form.get("guide")), "logo": _logo(form.get("logo"))}
        return clean
    if kind not in ("standard", "loop"):
        raise FormError("the station type must be standard or loop")
    clean = {"name": name, "number": number, "kind": kind, "logo": _logo(form.get("logo")),
             "caption_track": form.get("caption_track") is not False,
             "level_audio": form.get("level_audio") is not False}
    if kind == "standard":
        if form.get("schedule") is not None:
            try:
                clean["schedule"] = schedules.validate(form["schedule"])
            except schedules.ScheduleError as e:
                raise FormError(str(e))
        elif "days" in form and form["days"] is not None:
            days = form["days"]
            if not isinstance(days, dict) or set(days) != set(DAYS):
                raise FormError("a schedule that differs by day needs all seven days")
            week = {}
            for d in DAYS:
                try:
                    week[d] = _blocks(days[d])
                except FormError as e:
                    raise FormError(f"{d.capitalize()}: {e}")
            if all(week[d] == week[DAYS[0]] for d in DAYS):
                clean["blocks"] = week[DAYS[0]]          # every day the same: the plain form
            else:
                clean["days"] = week
        else:
            clean["blocks"] = _blocks(form.get("blocks"))
        inc = form.get("increment", 30)
        if inc not in INCREMENTS or isinstance(inc, bool):
            raise FormError(f"the time slot length must be one of {', '.join(map(str, INCREMENTS))} minutes")
        clean["increment"] = inc
        br = form.get("breaks", "end")
        if br not in BREAKS:
            raise FormError("breaks must be: after the show, interrupting it, or one in the middle")
        clean["breaks"] = br
        clean["snap"] = form.get("snap") is True          # move the break marks onto real black/silent gaps (breaksnap.py)
        clean["pack"] = form.get("pack") is not False     # short videos share a slot (fieldstation42/console_launch.py): on unless said no
        bump = form.get("bump", "bump")
        clean["bump"] = _list_name(bump) if bump else "bump"
        bump_end = form.get("bump_end")
        clean["bump_end"] = _list_name(bump_end) if bump_end else clean["bump"]          # blank: the same list for both
        clean["midcards"] = form.get("midcards") is not False     # opt in to the start/end of break cards: on unless said no
        clean["gen_cards"] = clean["midcards"] and form.get("gen_cards") is True   # a card drawn for this channel (midcards.py); off unless asked
        clean["gen_style"] = midcards.clean_style(form.get("gen_style"))              # which of the guide's looks it is drawn in
        clean["show_cards"] = _show_cards(form.get("show_cards"))
        com = form.get("commercials") or ""
        clean["commercials"] = _list_name(com) if com else ""
    else:
        loop = form.get("loop")
        if not isinstance(loop, dict):
            raise FormError("choose the list to repeat")
        clean["loop"] = {"list": _list_name(loop.get("list")), "shuffle": bool(loop.get("shuffle", False))}
    return clean


def make_cards_dir(lists_dir, start, end):
    """Make the hidden folder for a pair of lists (schedules.cards_folder): `pre` links to the start cards list and `post`
    to the end cards list. The same list for both needs no folder. Returns the folder's name."""
    name = schedules.cards_folder(start, end)
    if start == end:
        return name
    path = os.path.join(lists_dir, name)
    os.makedirs(path, exist_ok=True)
    for sub, target in (("pre", start), ("post", end)):
        link = os.path.join(path, sub)
        want = os.path.join("..", target)
        if os.path.islink(link) and os.readlink(link) == want:
            continue
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(want, link)
    return name


def lists_used(form):
    """Every list a validated form plays from, once each, in the order they first appear."""
    if form["kind"] == "guide":
        return []
    if form["kind"] == "loop":
        return [form["loop"]["list"]]
    if "schedule" in form:
        return schedules.lists_of(form["schedule"])
    sets = [form["blocks"]] if "blocks" in form else [form["days"][d] for d in DAYS]
    seen = []
    for blocks in sets:
        for b in blocks:
            seen += [x for x in b["lists"] if x not in seen]
    return seen


def to_conf(form, existing=None, today=None, shows=(), skip=(), premieres=None):
    """A validated form -> a FieldStation42 station config. An existing config keeps everything the form
    does not own (logos, images, parental controls...). A form with a `schedule` (seasons, special days,
    commercials by the hour) makes its keys for `today` (see schedules.py)."""
    conf = {k: copy.deepcopy(v) for k, v in (existing or {}).items() if not k.startswith("_")}
    for key in OWNED + (("date_overrides", "week_overrides") if "schedule" in form else ()) + (GUIDE_KEYS if form["kind"] == "guide" else ()):
        conf.pop(key, None)
    if form.get("caption_track") is False and form["kind"] != "guide":
        conf["wrapper_caption_track"] = False                # no caption track in the stream (on unless said no; tuner.py)
    if form.get("level_audio") is False and form["kind"] != "guide":
        conf["wrapper_level_audio"] = False                  # the volume is not evened out (on unless said no; loudness.py)
    if form.get("logo"):
        conf["wrapper_logo"] = form["logo"]                  # the picture a client shows beside the channel (channel_logo.py)
    conf["network_name"] = form["name"]
    conf["channel_number"] = form["number"]
    if form["kind"] == "guide":                          # drawn live by the tuner (guide_channel.py): no lists, catalog or schedule
        g = form["guide"]
        conf.pop("content_dir", None)
        conf["network_type"] = "guide"
        conf["messages"] = list(g["messages"])
        conf["play_sound"] = False        # FieldStation42 checks that a sound_to_play file exists when its console starts, and
        conf["scroll_speed"] = g["scroll_speed"]       # would not start at all if a media share were away: the music is ours
        conf["wrapper_guide"] = {"title": g["title"], "hours": g["hours"], "theme": g["theme"], "music": g["music"]}
        return conf
    if form["kind"] == "loop":
        conf["network_type"] = "loop"
        conf["content_dir"] = f"{CONTENT_DIR}/{form['loop']['list']}"
        conf["shuffle_loop"] = form["loop"]["shuffle"]
        return conf
    conf["network_type"] = "standard"
    conf["content_dir"] = CONTENT_DIR
    if form.get("midcards") is False:
        # the default cards are opt in: off, a break has none UNLESS its time (a calendar block) or its show names cards of
        # its own, which is an opt in for that video. The station's default folder is a name no choice can have, so the
        # engine side (console_launch.py) can tell "the default" from "chosen for this break".
        conf["bump_dir"] = OFF_CARDS
        conf["wrapper_start_cards"], conf["wrapper_end_cards"] = form["bump"], form.get("bump_end", form["bump"])
    elif form.get("gen_cards"):
        # the lists as chosen plus a card drawn for this channel (midcards.py): the station's own hidden break folder holds a
        # link to each list and to its clips, so the lists themselves are never changed
        conf["bump_dir"] = midcards.folder_name(form["name"])
        conf["wrapper_start_cards"], conf["wrapper_end_cards"] = form["bump"], form.get("bump_end", form["bump"])
        conf["wrapper_gen_cards"] = True
        if form.get("gen_style") and form["gen_style"] != midcards.DEFAULT_STYLE:
            conf["wrapper_gen_style"] = form["gen_style"]
    elif form.get("bump_end", form["bump"]) != form["bump"]:
        # two lists: the start-of-break cards and the end-of-break cards. FieldStation42 takes "pre" and "post" folders inside
        # one bump folder as the start and end cards, so the station gets a hidden folder of its own that holds the two
        # (made by make_cards_dir when the form is saved)
        conf["bump_dir"] = schedules.cards_folder(form["bump"], form["bump_end"])
        conf["wrapper_start_cards"], conf["wrapper_end_cards"] = form["bump"], form["bump_end"]
    else:
        conf["bump_dir"] = form["bump"]
    if form.get("show_cards"):
        conf["wrapper_show_cards"] = form["show_cards"]
        conf["tag_overrides"] = _tag_overrides(form["show_cards"])      # FieldStation42's own per-show override: this folder of cards
    if form.get("midcards") is False:
        conf["wrapper_mid_cards"] = False                 # no default cards (console_launch.py); absent means on
    conf["schedule_increment"] = form["increment"]
    conf["break_strategy"] = form["breaks"]
    conf.setdefault("off_air_video", OFF_AIR_CLIP)       # time with no slot assigned shows the test pattern (kept if one is set)
    if form.get("snap"):
        conf["wrapper_break_snap"] = True
    if form.get("pack") is False:
        conf["wrapper_pack_short"] = False                # absent means on
    if form["commercials"]:
        conf["commercial_free"] = False
        conf["commercial_dir"] = form["commercials"]
    else:
        # the default commercials are opt in: off, a time slot has no break UNLESS a commercials block (the calendar's tab)
        # names a list for it, which is an opt in for those hours. The engine is told commercials are on, with a folder
        # nothing else can have as the default, so console_launch.py can tell "the default" from "chosen for this slot":
        # a slot on the default gets no break (videos play back to back), a slot with its own list gets one as usual.
        conf["commercial_free"] = False
        conf["commercial_dir"] = COM_OFF
        conf["wrapper_no_breaks"] = True
    if "schedule" in form:
        from datetime import date
        conf.update(schedules.conf_parts(form["schedule"], today or date.today(), shows, skip, premieres))
        return conf

    def hours_of(blocks):
        hours = {}
        for b in blocks:
            tags = b["lists"][0] if len(b["lists"]) == 1 else list(b["lists"])
            for h in range(b["from"], b["to"]):
                hours[str(h)] = {"tags": tags}
                if any(n in shows for n in b["lists"]):
                    hours[str(h)]["sequence"] = schedules.SERIES
        return hours

    if "blocks" in form:                                  # every day the same
        conf["day_templates"] = {"daily": hours_of(form["blocks"])}
        for d in DAYS:
            conf[d] = "daily"
        return conf
    templates, names = {}, {}                             # days that match share one template
    for d in DAYS:
        key = json.dumps(form["days"][d], sort_keys=True)
        if key not in names:
            names[key] = f"schedule{len(names) + 1}"
            templates[names[key]] = hours_of(form["days"][d])
        conf[d] = names[key]
    conf["day_templates"] = templates
    return conf


def parse(conf):
    """A FieldStation42 station config -> (form, None), or (None, why the form can't hold it).

    Never raises: the Stations page reads every station through this, and one odd config must not be able to
    take the whole page down."""
    try:
        return _parse(conf)
    except Exception:
        return None, "its settings are in a shape the form does not understand"


def _parse(conf):
    if not isinstance(conf, dict):
        return None, "this station's settings could not be read"
    name, number = conf.get("network_name"), conf.get("channel_number")
    if not isinstance(name, str) or isinstance(number, bool) or not isinstance(number, int):
        return None, "this station has no usable name or channel number"
    kind = conf.get("network_type", "standard")
    if kind == "loop":
        cd = conf.get("content_dir", "")
        prefix = CONTENT_DIR + "/"
        if not cd.startswith(prefix) or "/" in cd[len(prefix):] or not cd[len(prefix):]:
            return None, f"this loop plays {cd or 'a folder'}, which is not one of your lists"
        return {"name": name, "number": number, "kind": "loop", "logo": conf.get("wrapper_logo") or "", "caption_track": conf.get("wrapper_caption_track") is not False, "level_audio": conf.get("wrapper_level_audio") is not False,
                "loop": {"list": cd[len(prefix):], "shuffle": bool(conf.get("shuffle_loop", False))}}, None
    if kind == "guide":
        extra = conf.get("wrapper_guide") if isinstance(conf.get("wrapper_guide"), dict) else {}
        msgs = conf.get("messages") if isinstance(conf.get("messages"), list) else []
        music = extra.get("music") if isinstance(extra.get("music"), str) else (
            conf.get("sound_to_play") if conf.get("play_sound") and isinstance(conf.get("sound_to_play"), str) else "")
        speed = conf.get("scroll_speed", 1.0)
        return {"name": name, "number": number, "kind": "guide", "logo": conf.get("wrapper_logo") or "", "guide": {
            "title": extra.get("title") if isinstance(extra.get("title"), str) and extra.get("title").strip() else "Channel Guide",
            "hours": extra.get("hours") if isinstance(extra.get("hours"), int) and not isinstance(extra.get("hours"), bool) and 1 <= extra.get("hours") <= 6 else 3,
            "scroll_speed": float(speed) if isinstance(speed, (int, float)) and not isinstance(speed, bool) and 0.1 <= speed <= 5 else 1.0,
            "messages": [m for m in msgs if isinstance(m, str)][:20], "music": music,
            "theme": extra.get("theme") if extra.get("theme") in guide_channel.THEMES else guide_channel.DEFAULT_THEME}}, None
    if kind != "standard":
        return None, f"a '{kind}' station is edited in the FieldStation42 console"
    if conf.get("content_dir") != CONTENT_DIR:
        return None, f"this station plays from {conf.get('content_dir')}, not from your lists"
    show_cards = show_cards_of(conf)
    for k in ADVANCED_KEYS:
        if conf.get(k) and not (k == "tag_overrides" and show_cards is not None):
            return None, f"it uses an advanced setting ({k})"
    templates = conf.get("day_templates")
    if not isinstance(templates, dict) or not templates:
        return None, "it has no day templates"
    if not all(isinstance(conf.get(d), str) for d in DAYS):
        return None, "some of its days have a schedule of their own instead of using a day template"
    used = {conf[d] for d in DAYS}
    if not all(u in templates for u in used):
        return None, "some of its days are not set to one of its day templates"
    if set(templates) - used:
        return None, "it has day templates that no day uses"
    week = {}
    for d in DAYS:
        blocks, why = _blocks_of_template(templates[conf[d]])
        if blocks is None:
            return None, why
        week[d] = blocks
    same = all(week[d] == week[DAYS[0]] for d in DAYS)
    commercials = "" if conf.get("commercial_free", True) or conf.get("commercial_dir") == COM_OFF else conf.get("commercial_dir", "")
    if not conf.get("commercial_free", True) and conf.get("commercial_dir") != COM_OFF and not commercials:
        return None, "commercials are on but no commercial folder is set"
    inc = conf.get("schedule_increment", 30)
    if inc not in INCREMENTS:
        return None, f"its time slots are {inc} minutes, which the form does not offer"
    breaks = conf.get("break_strategy", "standard")           # FieldStation42's own default when none is set
    if breaks not in BREAKS:
        return None, f"its commercial breaks are set to '{breaks}', which the form does not offer"
    form = {"name": name, "number": number, "kind": "standard", "logo": conf.get("wrapper_logo") or "", "caption_track": conf.get("wrapper_caption_track") is not False, "level_audio": conf.get("wrapper_level_audio") is not False, "increment": inc, "breaks": breaks,
            "bump": conf.get("wrapper_start_cards") or (conf.get("bump_dir") if conf.get("bump_dir") != OFF_CARDS else None) or "bump", "bump_end": conf.get("wrapper_end_cards") or conf.get("wrapper_start_cards") or (conf.get("bump_dir") if conf.get("bump_dir") != OFF_CARDS else None) or "bump",
            "commercials": commercials, "snap": conf.get("wrapper_break_snap") is True, "midcards": conf.get("wrapper_mid_cards") is not False, "show_cards": show_cards or [],
            "gen_cards": conf.get("wrapper_gen_cards") is True, "gen_style": midcards.clean_style(conf.get("wrapper_gen_style")), "pack": conf.get("wrapper_pack_short") is not False}
    form["blocks" if same else "days"] = week[DAYS[0]] if same else week
    return form, None


def parse_base(conf):
    """(form, None) with only the station's own settings (name, number, increment, breaks, bump, commercials), for a
    station whose schedule is kept by the wrapper (schedules.py) and read from there, or (None, why)."""
    try:
        name, number = conf.get("network_name"), conf.get("channel_number")
        if not isinstance(name, str) or isinstance(number, bool) or not isinstance(number, int):
            return None, "this station has no usable name or channel number"
        if conf.get("network_type", "standard") != "standard" or conf.get("content_dir") != CONTENT_DIR:
            return None, "this station is not a standard station that plays from your lists"
        show_cards = show_cards_of(conf)
        for k in ADVANCED_KEYS:
            if k not in ("date_overrides", "week_overrides") and conf.get(k) and not (k == "tag_overrides" and show_cards is not None):
                return None, f"it uses an advanced setting ({k})"
        commercials = "" if conf.get("commercial_free", True) or conf.get("commercial_dir") == COM_OFF else conf.get("commercial_dir", "")
        inc = conf.get("schedule_increment", 30)
        breaks = conf.get("break_strategy", "standard")
        if inc not in INCREMENTS or breaks not in BREAKS:
            return None, "its slot length or break style is not one the form offers"
        return {"name": name, "number": number, "kind": "standard", "logo": conf.get("wrapper_logo") or "", "caption_track": conf.get("wrapper_caption_track") is not False, "level_audio": conf.get("wrapper_level_audio") is not False, "increment": inc, "breaks": breaks,
                "bump": conf.get("wrapper_start_cards") or (conf.get("bump_dir") if conf.get("bump_dir") != OFF_CARDS else None) or "bump",
                "bump_end": conf.get("wrapper_end_cards") or conf.get("wrapper_start_cards") or (conf.get("bump_dir") if conf.get("bump_dir") != OFF_CARDS else None) or "bump", "commercials": commercials,
                "snap": conf.get("wrapper_break_snap") is True, "midcards": conf.get("wrapper_mid_cards") is not False, "show_cards": show_cards or [],
                "gen_cards": conf.get("wrapper_gen_cards") is True, "gen_style": midcards.clean_style(conf.get("wrapper_gen_style")), "pack": conf.get("wrapper_pack_short") is not False}, None
    except Exception:
        return None, "its settings are in a shape the form does not understand"


def _blocks_of_template(tmpl):
    """One day template -> (blocks, None), or (None, why it is more than the form can hold)."""
    if not isinstance(tmpl, dict) or set(tmpl) != {str(h) for h in range(24)}:
        return None, "its schedule does not have one slot for every hour"
    per_hour = []
    for h in range(24):
        slot = tmpl[str(h)]
        if not isinstance(slot, dict) or set(slot) - SLOT_KEYS_OK or "tags" not in slot or slot.get("sequence", schedules.SERIES) != schedules.SERIES:
            return None, f"the slot at hour {h} uses advanced settings"
        tags = slot["tags"]
        tags = [tags] if isinstance(tags, str) else tags
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t for t in tags):
            return None, f"the slot at hour {h} has no usable list"
        per_hour.append(tags)
    blocks, start = [], 0
    for h in range(1, 25):
        if h == 24 or per_hour[h] != per_hour[start]:
            blocks.append({"from": start, "to": h, "lists": per_hour[start]})
            start = h
    return blocks, None
