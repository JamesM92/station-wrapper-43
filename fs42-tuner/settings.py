"""Settings you change from the dashboard, kept in the config volume (settings.json).

Today that is the TubeArchivist conversion: whether it runs, which folder is your TubeArchivist
library, and how often. The TA_ENABLED / TA_LIBRARY / TA_INTERVAL_MINUTES environment variables
still work, as the starting values: a setting saved from the dashboard wins over them, one key at
a time. The dashboard (tuner.py) writes the file and the conversion job (ta-nfo/service.py) reads
it, so a change takes effect without restarting the container.
"""
import json
import os
import re
import tempfile

PATH = os.environ.get("FS42_SETTINGS", "/config/settings.json")
STATUS_PATH = os.environ.get("FS42_TA_STATUS", "/tmp/ta-status.json")  # nosec B108 - signal files between the container's own services     # the job's own state, not a setting
RUN_NOW = os.environ.get("FS42_TA_NOW", "/tmp/ta.now")  # nosec B108                                                 # "convert now": the job looks for this
FARM_NOW = os.environ.get("FS42_FARM_NOW", "/tmp/farm.now")  # nosec B108                                               # "new channels": the farm job looks for this

DEFAULT_TA = {"enabled": False, "libraries": ["/mnt/media/tubearchivist"], "interval_minutes": 1440}
MAX_LIBRARIES = 12
VIEW_SUFFIX = "-nfo"                                                      # a converted library shows as <its folder name>-nfo
INTERVAL_RANGE = (15, 10080)                                              # every 15 minutes to once a week
TRUE = ("1", "true", "yes", "on")


class SettingsError(ValueError):
    pass


def load(path=None):
    """The saved settings, or {} when there are none or the file is unreadable. Never raises."""
    try:
        with open(path or PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def view_names(libraries):
    """{library path: name of its converted folder}: the folder's own name plus -nfo (youtube -> youtube-nfo).
    Two libraries with the same folder name get -2, -3 ... so the names are unique and never change while the
    list keeps its order."""
    out, used = {}, set()
    for lib in libraries:
        base = (os.path.basename(lib.rstrip("/")) or "library") + VIEW_SUFFIX
        name, n = base, 1
        while name.casefold() in used:
            n += 1
            name = f"{base}-{n}"
        used.add(name.casefold())
        out[lib] = name
    return out


def _clean_libraries(value):
    """A list of absolute folder paths, in order, without repeats; None if it is not such a list."""
    if not isinstance(value, list):
        return None
    out = []
    for x in value:
        if not isinstance(x, str) or "\0" in x or not x.strip().startswith("/"):
            return None
        p = os.path.normpath(x.strip())
        if p not in out:
            out.append(p)
    return out[:MAX_LIBRARIES]


def get_ta(env=None, path=None):
    """The TubeArchivist settings in force, and where each came from."""
    env = os.environ if env is None else env
    out, source = dict(DEFAULT_TA), {k: "default" for k in DEFAULT_TA}
    if env.get("TA_ENABLED", "").strip():
        out["enabled"], source["enabled"] = env["TA_ENABLED"].strip().lower() in TRUE, "env"
    if env.get("TA_LIBRARY", "").strip():       # one folder, or several separated by commas or colons
        libs = _clean_libraries([p for p in re.split(r"[,:]", env["TA_LIBRARY"]) if p.strip()])
        if libs:
            out["libraries"], source["libraries"] = libs, "env"
    try:
        if env.get("TA_INTERVAL_MINUTES", "").strip():
            out["interval_minutes"], source["interval_minutes"] = int(env["TA_INTERVAL_MINUTES"]), "env"
    except ValueError:
        pass
    saved = load(path).get("ta")
    if isinstance(saved, dict):
        if isinstance(saved.get("enabled"), bool):
            out["enabled"], source["enabled"] = saved["enabled"], "saved"
        libs = _clean_libraries(saved.get("libraries"))
        if libs is None and isinstance(saved.get("library"), str):          # saved by an older version: one folder
            libs = _clean_libraries([saved["library"]])
        if libs:
            out["libraries"], source["libraries"] = libs, "saved"
        iv = saved.get("interval_minutes")
        if isinstance(iv, int) and not isinstance(iv, bool) and INTERVAL_RANGE[0] <= iv <= INTERVAL_RANGE[1]:
            out["interval_minutes"], source["interval_minutes"] = iv, "saved"
    lo, hi = INTERVAL_RANGE
    out["interval_minutes"] = min(hi, max(lo, out["interval_minutes"]))
    out["views"] = [{"library": p, "name": n} for p, n in view_names(out["libraries"]).items()]
    out["library"] = out["libraries"][0]                                      # the first one, for older callers
    return {**out, "source": source}


def set_ta(values, path=None):
    """Validate and save some or all of enabled / library / interval_minutes. Returns the new settings."""
    path = path or PATH
    clean = {}
    if "enabled" in values:
        if not isinstance(values["enabled"], bool):
            raise SettingsError("enabled must be on or off")
        clean["enabled"] = values["enabled"]
    if "libraries" in values:
        libs = _clean_libraries(values["libraries"])
        if libs is None or not libs:
            raise SettingsError("the libraries must be a list of folder paths, at least one")
        if len(values["libraries"]) > MAX_LIBRARIES:
            raise SettingsError(f"at most {MAX_LIBRARIES} libraries")
        clean["libraries"] = libs
    elif "library" in values:                   # the old single-folder form
        lib = values["library"]
        if not isinstance(lib, str) or "\0" in lib or (lib.strip() and not lib.strip().startswith("/")):
            raise SettingsError("the library must be a folder path")
        if lib.strip():
            clean["libraries"] = [os.path.normpath(lib.strip())]
    if "interval_minutes" in values:
        iv = values["interval_minutes"]
        if isinstance(iv, bool) or not isinstance(iv, int) or not INTERVAL_RANGE[0] <= iv <= INTERVAL_RANGE[1]:
            raise SettingsError(f"how often must be between {INTERVAL_RANGE[0]} and {INTERVAL_RANGE[1]} minutes")
        clean["interval_minutes"] = iv
    data = load(path)
    ta = data.get("ta") if isinstance(data.get("ta"), dict) else {}
    old = ta.pop("library", None)               # an older version saved one folder: keep it as a list of one
    if old and "libraries" not in ta and _clean_libraries([old]):
        ta["libraries"] = _clean_libraries([old])
    ta.update(clean)
    data["ta"] = ta
    _save(path, data)
    return get_ta(path=path)


def read_status():
    try:
        with open(STATUS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(**fields):
    """The job's state for the dashboard. Written atomically; losing a write is harmless."""
    try:
        data = read_status()
        data.update(fields)
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass


def _save(path, data):
    """Write the settings file atomically, readable and editable from the host, owned like the config folder."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o666)  # nosec B103 - a file in the config volume, editable from the host by whoever owns the folder            # the config volume is yours: you can read and edit it from the host
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    if os.geteuid() == 0:               # we run as root; hand the file to whoever owns the config folder
        try:
            ref = os.stat(folder)
            os.chown(path, ref.st_uid, ref.st_gid)
        except OSError:
            pass


# ---- the names videos are shown under (titles.py)
def get_titles(path=None):
    """{"episode_names": bool}: whether an episode is named "Show: Episode" (on, the default) or just "Show" (off)."""
    saved = load(path).get("titles")
    if isinstance(saved, dict) and isinstance(saved.get("episode_names"), bool):
        return {"episode_names": saved["episode_names"]}
    return {"episode_names": True}


def set_titles(values, path=None):
    path = path or PATH
    if "episode_names" in values and not isinstance(values["episode_names"], bool):
        raise SettingsError("episode names must be on or off")
    data = load(path)
    titles = data.get("titles") if isinstance(data.get("titles"), dict) else {}
    if "episode_names" in values:
        titles["episode_names"] = values["episode_names"]
    data["titles"] = titles
    _save(path, data)
    return get_titles(path)


# ---- captions: the one language the container uses for the captions it carries or draws (captions.py)
def get_captions(path=None):
    """{"language": "en"}: the language of the caption track a video is asked for, in the stream and drawn on the picture."""
    saved = load(path).get("captions")
    lang = saved.get("language") if isinstance(saved, dict) else None
    if isinstance(lang, str) and re.fullmatch(r"[a-z]{2,3}", lang):
        return {"language": lang}
    return {"language": "en"}


def set_captions(values, path=None):
    path = path or PATH
    lang = values.get("language")
    if not isinstance(lang, str) or not re.fullmatch(r"[A-Za-z]{2,3}", lang.strip()):
        raise SettingsError("the caption language is two or three letters, such as en or ja")
    data = load(path)
    data["captions"] = {"language": lang.strip().lower()}
    _save(path, data)
    return get_captions(path)


# ---- the volume target (loudness.py)
def get_loudness(path=None):
    """{"target": LUFS}: the loudness shows and commercials are brought to (-23 is broadcast television)."""
    saved = load(path).get("loudness")
    t = saved.get("target") if isinstance(saved, dict) else None
    if isinstance(t, (int, float)) and not isinstance(t, bool) and -30 <= t <= -10:
        return {"target": float(t)}
    return {"target": -23.0}


def set_loudness(values, path=None):
    path = path or PATH
    t = values.get("target")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not -30 <= t <= -10:
        raise SettingsError("the volume target is between -30 and -10 LUFS (-23 is broadcast television)")
    data = load(path)
    data["loudness"] = {"target": float(t)}
    _save(path, data)
    return get_loudness(path)


# ---- the schedule keeper: keeps every station's schedule running a few days ahead
DEFAULT_KEEPER = {"enabled": True, "days_ahead": 7}
DAYS_AHEAD_RANGE = (1, 30)
OFF = ("0", "false", "no", "off")


def get_keeper(env=None, path=None):
    """The keeper's settings in force, and where each came from (saved > environment > default)."""
    env = os.environ if env is None else env
    out, source = dict(DEFAULT_KEEPER), {k: "default" for k in DEFAULT_KEEPER}
    if env.get("SCHEDULE_KEEPER", "").strip():
        out["enabled"], source["enabled"] = env["SCHEDULE_KEEPER"].strip().lower() not in OFF, "env"
    try:
        if env.get("KEEPER_DAYS_AHEAD", "").strip():
            out["days_ahead"], source["days_ahead"] = int(env["KEEPER_DAYS_AHEAD"]), "env"
    except ValueError:
        pass
    saved = load(path).get("keeper")
    if isinstance(saved, dict):
        if isinstance(saved.get("enabled"), bool):
            out["enabled"], source["enabled"] = saved["enabled"], "saved"
        d = saved.get("days_ahead")
        if isinstance(d, int) and not isinstance(d, bool) and DAYS_AHEAD_RANGE[0] <= d <= DAYS_AHEAD_RANGE[1]:
            out["days_ahead"], source["days_ahead"] = d, "saved"
    lo, hi = DAYS_AHEAD_RANGE
    out["days_ahead"] = min(hi, max(lo, out["days_ahead"]))
    return {**out, "source": source}


def set_keeper(values, path=None):
    """Validate and save enabled / days_ahead. Returns the new settings."""
    path = path or PATH
    clean = {}
    if "enabled" in values:
        if not isinstance(values["enabled"], bool):
            raise SettingsError("enabled must be on or off")
        clean["enabled"] = values["enabled"]
    if "days_ahead" in values:
        d = values["days_ahead"]
        if isinstance(d, bool) or not isinstance(d, int) or not DAYS_AHEAD_RANGE[0] <= d <= DAYS_AHEAD_RANGE[1]:
            raise SettingsError(f"days ahead must be between {DAYS_AHEAD_RANGE[0]} and {DAYS_AHEAD_RANGE[1]}")
        clean["days_ahead"] = d
    data = load(path)
    keeper = data.get("keeper") if isinstance(data.get("keeper"), dict) else {}
    keeper.update(clean)
    data["keeper"] = keeper
    _save(path, data)
    return get_keeper(path=path)
