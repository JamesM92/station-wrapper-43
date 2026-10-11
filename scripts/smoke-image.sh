#!/usr/bin/env bash
# Smoke-test the built image: a check, not a build. Used by scripts/preflight.sh and by
# dev-image.yml / release-image.yml between the build and the push, so a broken image is
# never published.
#
# Usage: scripts/smoke-image.sh <image>
#
# Everything runs INSIDE the container, with scripts fed on stdin. A bind mount of the
# workspace would not resolve when the Docker daemon is remote, as it is in CI.
set -euo pipefail

image="${1:?usage: smoke-image.sh <image>}"
fail() { echo "smoke FAILED: $1" >&2; [ -n "${2:-}" ] && echo "$2" >&2; exit 1; }

# 1. Everything the image runs is there and imports.
out="$(docker run --rm -i --entrypoint sh "$image" 2>&1 <<'EOS' || true
set -e
python3 --version
python3 -c "import fastapi, uvicorn, ffmpeg, jsonschema, mutagen; print('console deps ok')"
cd /opt/tuner && FS42_TUNER_CONF=/opt/tuner-config.default.json python3 -c "import tuner, resolver, guide, library; print('tuner imports ok')"
cd /opt/sync && python3 -c "import pool, sync; print('sync imports ok')"
python3 -c "import sys; sys.path.insert(0, '/opt/supervisor'); import supervisor; print('supervisor imports ok')"
python3 /opt/ta-nfo/ta_nfo.py --version
ffmpeg -version | head -1
ffprobe -version | head -1
test -s /opt/tuner/static/mpegts.min.js && echo "player script present"
test -x /usr/bin/tini && echo "tini present"
python3 -c "import json; json.load(open('/opt/tuner-config.default.json')); print('default tuner config is valid json')"
test ! -e /app/confs && test ! -e /app/runtime && echo "confs and runtime are left for the config volume's links"
# Docker copies an image folder's ownership onto a volume's host folder the first time it is used, which
# would make the user's own folders root's. So the mount points must not exist in the image.
test ! -e /config && test ! -e /app/catalog && echo "the volume mount points are not in the image (their owner stays yours)"
echo "tuner-config-path=$FS42_TUNER_CONF"
EOS
)"
for want in "console deps ok" "tuner imports ok" "sync imports ok" "supervisor imports ok" "ta_nfo.py 0." \
            "ffmpeg version" "ffprobe version" "player script present" "tini present" "default tuner config is valid" \
            "confs and runtime are left for" "volume mount points are not in the image" "tuner-config-path=/config/tuner/config.json"; do
  grep -q "$want" <<<"$out" || fail "the image is missing something ('$want'):" "$out"
done
echo "image contents: ok"

# 2. The catalog must not be an SMB share: make /app/catalog report cifs and check that
#    the entrypoint refuses (exit 78 and the plain message).
out="$(docker run --rm -i --entrypoint sh "$image" 2>&1 <<'EOS' || true
mkdir -p /shim /app/catalog
cat > /shim/stat <<'SHIM'
#!/bin/bash
for a in "$@"; do last="$a"; done
if [ "$last" = /app/catalog ]; then echo cifs; else exec /usr/bin/stat "$@"; fi
SHIM
chmod +x /shim/stat
PATH=/shim:$PATH /entrypoint.sh
echo "entrypoint-exit=$?"
EOS
)"
grep -q 'entrypoint-exit=78' <<<"$out" && grep -q 'REFUSING TO START' <<<"$out" \
  || fail "the entrypoint did not refuse a catalog on an SMB share:" "$out"
echo "refuses an SMB catalog: ok"

# 3. The whole container, as it runs, with the three kinds of volume it is meant to have:
#    config and catalog (local folders) and SEPARATE media volumes, one per folder, mounted at
#    /mnt/media/<folder>, as a real deployment mounts them. Named volumes, so this works when the
#    Docker daemon is remote too. Checks: the entrypoint links runtime/ and confs/ into the config
#    volume and creates the tuner's config there; both ports answer; the sync service builds the
#    pool inside the catalog from every media volume; the catalog's top level has no
#    whole-folder links (an old one is removed, the user's own stays); the TubeArchivist job reports a
#    library that is not mounted instead of dying, and the dashboard's API switches it off; SIGTERM stops it all.
tag="station-wrapper-43-smoke-$$"
vols="$tag-config $tag-catalog $tag-movies $tag-anime $tag-tanfo"
cleanup() { docker volume rm -f $vols >/dev/null 2>&1 || true; }
trap cleanup EXIT
for v in $vols; do docker volume create "$v" >/dev/null; done
docker run --rm --entrypoint sh -v "$tag-movies:/m" -v "$tag-anime:/a" -v "$tag-tanfo:/t" "$image" -c '
  mkdir -p /m/Film /a/Show /t/Chan
  touch /m/Film/Film.mkv /m/Film/Film.nfo /m/Film/poster.jpg /a/Show/e1.mp4 /t/Chan/x.mp4' >/dev/null
out="$(docker run --rm -i --entrypoint sh \
        -v "$tag-config:/config" -v "$tag-catalog:/app/catalog" \
        -v "$tag-movies:/mnt/media/movies:ro" -v "$tag-anime:/mnt/media/anime:ro" -v "$tag-tanfo:/mnt/media/tanfo:ro" \
        "$image" 2>&1 <<'EOS' || true
export DASHBOARD_LOGIN=0 KEEPER_FIRST_DELAY_SECONDS=5 SYNC_ENABLED=1 TA_ENABLED=1 TA_LIBRARY=/mnt/media/not-there TZ=America/Toronto
# what an earlier version left behind (must be removed) and a link of the user's own (must stay)
ln -s /mnt/media/anime/ /app/catalog/anime
ln -s /somewhere/mine /app/catalog/mine
/entrypoint.sh > /tmp/container.log 2>&1 &
PID=$!
python3 - <<'PY'
import time, urllib.request
def up(url):
    try:
        urllib.request.urlopen(url, timeout=2).read(); return True
    except Exception:
        return False
end = time.time() + 60
while time.time() < end:
    if up("http://127.0.0.1:4242/stations") and up("http://127.0.0.1:5004/channels.json"):
        print("both-ports-up"); break
    time.sleep(1)
else:
    print("PORTS-NEVER-CAME-UP")
PY
sleep 3
echo "config=$(ls /config | tr '\n' ' ')"
echo "tuner-config=$(ls /config/tuner | tr '\n' ' ')"
echo "runtime-link=$(readlink /app/runtime)"
echo "confs-link=$(readlink /app/confs)"
echo "link=$(readlink /app/catalog/media-links/movies/Film/Film.mkv)"
echo "link2=$(readlink /app/catalog/media-links/anime/Show/e1.mp4)"
echo "pool=$(cd /app/catalog/media-links && find . -type l | sort | tr '\n' ' ')"
echo "catalog-top=$(cd /app/catalog && for e in *; do if [ -L "$e" ]; then echo "$e->link"; else echo "$e"; fi; done | sort | tr '\n' ' ')"
echo "bump=$(ls /app/catalog/by-genre/bump | tr '\n' ' ')"
echo "bump-real=$(find /app/catalog/by-genre/bump -type f ! -type l | wc -l)"
echo "bump-owner=$(stat -c %u:%g /app/catalog/by-genre/bump) catalog-owner=$(stat -c %u:%g /app/catalog)"
# the channel tools, run inside the container (no clone, no pip on the host)
fs42-channels init > /tmp/init.out 2>&1; echo "init-exit=$?"
echo "groups-written=$(test -f /config/groups.toml && echo yes || echo no)"
fs42-channels build --dry-run > /tmp/build.out 2>&1; echo "build-exit=$?"
echo "build-says=$(tail -1 /tmp/build.out)"
# the file manager (Files page) API, against the real container: make a list, link a folder into it, take it out
python3 - <<'PY'
import json, os, time, urllib.error, urllib.request
def call(path, body=None):
    # the dashboard restarts itself when a station appears (the test station does), so retry through that
    for attempt in range(40):
        try:
            req = urllib.request.Request("http://127.0.0.1:5004" + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"} if body is not None else {})
            return json.load(urllib.request.urlopen(req, timeout=10))
        except (urllib.error.URLError, ConnectionError):
            time.sleep(1)
    raise RuntimeError("the dashboard did not come back")
info = call("/api/files/info")
call("/api/files/create-tag", {"name": "SmokeList"})
added = call("/api/files/add", {"tag": "SmokeList", "sources": [{"root": "media", "path": "movies/Film"}]})
link = "/app/catalog/by-genre/SmokeList/Film/Film.mkv"
print("files-api=%s,%s,%s,%s" % (info["edit"], info["tags"], added["added"], os.path.islink(link)))
call("/api/files/delete-tag", {"tag": "SmokeList"})
print("files-gone=%s media-kept=%s" % (not os.path.exists("/app/catalog/by-genre/SmokeList"), os.path.isfile("/mnt/media/movies/Film/Film.mkv")))
PY
# the TubeArchivist panel's API: the job (switched on here by the old TA_* variables, with a library that is not
# there) reports the problem instead of dying, and switching it off from the dashboard takes effect with no restart
python3 - <<'PY'
import json, time, urllib.error, urllib.request
def call(path, body=None):
    # the dashboard restarts itself when a station appears (the test station does), so retry through that
    for attempt in range(40):
        try:
            req = urllib.request.Request("http://127.0.0.1:5004" + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"} if body is not None else {})
            return json.load(urllib.request.urlopen(req, timeout=10))
        except (urllib.error.URLError, ConnectionError):
            time.sleep(1)
    raise RuntimeError("the dashboard did not come back")
def wait(state, secs=20):
    end = time.time() + secs
    while time.time() < end:
        st = call("/api/ta/status")
        if st["job"].get("state") == state:
            return st
        time.sleep(0.5)
    return st
st = wait("error")
print("ta-error=%s,%s,%s" % (st["settings"]["enabled"], st["settings"]["source"]["enabled"], st["job"].get("state")))
call("/api/ta/settings", {"enabled": False})
print("ta-off=%s" % wait("disabled")["job"].get("state"))
PY
# a brand new install puts a test station (channel 2) on air by itself
python3 - <<'PY'
import json, time, urllib.request
import os
end, seen = time.time() + 240, []
# on air as soon as the first day is built; the job then adds the rest of its days and only then writes its flag
while time.time() < end and not (seen and os.path.exists("/config/.test-station-done")):
    try:
        seen = [c for c in json.load(urllib.request.urlopen("http://127.0.0.1:5004/channels.json", timeout=5)) if c["channel"] == "2"]
    except Exception:
        pass
    time.sleep(2)
print("test-station=" + (f"{seen[0]['station']},{seen[0]['on_air']}" if seen else "missing"))
# and FieldStation42's OWN console must see its schedule (it kept saying "no schedule" when days were added by a
# separate process; the console's own add-day route makes it reload)
# (the console briefly answers 500 while it reloads after a day is added, which the keeper does at start: retry)
result = "error"
for attempt in range(30):
    try:
        s = json.load(urllib.request.urlopen("http://127.0.0.1:4242/summary/schedules/Test%20Pattern", timeout=10))["schedule_summary"]
        result = "yes" if s.get("start") and s.get("end") else "no"
        break
    except Exception as e:
        result = "error " + str(e)
        time.sleep(1)
print("console-schedule=" + result)
PY
# our own Stations and Schedule views, reading the real console and the real schedule database
python3 - <<'PY'
import json, time, urllib.error, urllib.request
def get(path):
    for attempt in range(40):
        try:
            return json.load(urllib.request.urlopen("http://127.0.0.1:5004" + path, timeout=15))
        except (urllib.error.URLError, ConnectionError):
            time.sleep(1)
    raise RuntimeError("the dashboard did not come back")
rows = get("/api/stations")["stations"]
print("stations-api=" + ",".join(f"{r['name']}:{r['number']}:{r['editable']}:{r['on_air']}" for r in rows if r["name"] != "Channel Guide"))
# the guide channel a new install gets too: channel 1, its form reads back, default style
guide = [r for r in rows if r["name"] == "Channel Guide"]
gform = get("/api/stations/get?name=Channel%20Guide")["form"] if guide else {}
import subprocess
sm = subprocess.run(["python3", "-c", "from fs42.station_manager import StationManager; StationManager(); print('ok')"], cwd="/app", capture_output=True, text=True)
print("station-manager=" + ("ok" if sm.stdout.strip().endswith("ok") else "failed: " + sm.stderr.strip()[-200:]))      # what the console does at every start
print("guide-station=%s" % (f"{guide[0]['number']}:{guide[0]['editable']}:{gform.get('kind')}:{gform['guide']['theme']}" if guide else "missing"))
sched = get("/api/schedule?name=Test%20Pattern")
print("schedule-api=%d:%s" % (len(sched["blocks"]), sched["on_air"]))
got = get("/api/stations/get?name=Test%20Pattern")
print("station-form=%s:%s" % (got["form"]["kind"], got["form"]["loop"]["list"]))
import urllib.parse
blk = get("/api/schedule/block?name=Test%20Pattern&start=" + urllib.parse.quote(sched["blocks"][0]["start"]))
print("block-api=%d:%s:%s" % (blk["total"], blk["entries"][0]["file"], blk["entries"][0]["type"]))
try:
    req = urllib.request.Request("http://127.0.0.1:5004/api/stations/rename", data=json.dumps({"name": "No Such Station", "to": "x"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15)
    print("rename-api=accepted")
except urllib.error.HTTPError as e:
    print("rename-api=%d" % e.code)
codes = []
for page in ("stations", "station", "schedule", "files"):
    codes.append(str(urllib.request.urlopen(f"http://127.0.0.1:5004/{page}.html", timeout=10).status))
print("pages=" + ",".join(codes))
PY
# the schedule keeper: on by default, and it has made a pass over the stations
python3 - <<'PY'
import json, time, urllib.error, urllib.request
def get(path):
    for attempt in range(40):
        try:
            return json.load(urllib.request.urlopen("http://127.0.0.1:5004" + path, timeout=10))
        except (urllib.error.URLError, ConnectionError):
            time.sleep(1)
    raise RuntimeError("the dashboard did not come back")
end = time.time() + 90
k = get("/api/keeper")
while time.time() < end and not k["status"]["last_pass"]:
    time.sleep(2)
    k = get("/api/keeper")
print("keeper=%s,%s,%s" % (k["settings"]["enabled"], k["settings"]["days_ahead"], "passed" if k["status"]["last_pass"] else "never"))
PY
# the login, in the packaged code: a second dashboard on another port with the login ON (the main one has it
# off so the checks above can call its API). Locked until a password is set, setting one logs you in, and the
# lineup other clients use stays open.
python3 - <<'PY'
import http.client, json, os, subprocess, time
conf = json.load(open("/config/tuner/config.json"))
conf["port"], conf["advertise_url"] = 5099, "http://127.0.0.1:5099"
json.dump(conf, open("/tmp/login-test.json", "w"))
env = {**os.environ, "FS42_TUNER_CONF": "/tmp/login-test.json", "DASHBOARD_LOGIN": "1", "FS42_SETTINGS": "/tmp/login-test-settings.json",
       "FS42_TA_STATUS": "/tmp/login-test-ta.json", "KEEPER_FIRST_DELAY_SECONDS": "99999"}
proc = subprocess.Popen(["python3", "tuner.py"], cwd="/opt/tuner", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def req(method, path, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", 5099, timeout=10)
    h = {"Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = "sw43_session=" + cookie
    c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=h)
    r = c.getresponse()
    r.read()
    return r
out = []
try:
    for _ in range(60):
        try:
            req("GET", "/discover.json")
            break
        except OSError:
            time.sleep(0.5)
    out.append(req("GET", "/api/stations").status)                 # locked
    out.append(req("GET", "/stations.html").status)                # sent to the login page
    out.append(req("GET", "/lineup.json").status)                  # what Jellyfin and other clients use stays open
    r = req("POST", "/api/auth/setup", {"password": "smoke test password"})
    out.append("setup%d" % r.status)
    cookie = (r.getheader("Set-Cookie") or "").split(";")[0].split("=", 1)[-1]
    out.append(req("GET", "/api/files/info", cookie=cookie).status)    # logged in
    out.append(req("GET", "/api/files/info").status)                   # a stranger still is not
finally:
    proc.terminate()
print("login=" + ",".join(str(x) for x in out))
PY
echo "test-flag=$(test -f /config/.test-station-done && echo yes || echo no) test-clip=$(test -f '/app/catalog/by-genre/Test Pattern/test-pattern.mp4' && echo yes || echo no)"
echo "dashboard=$(python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5004/channels.json').read().decode())")"
kill -TERM $PID
T0=$(date +%s)
wait $PID
echo "exit=$? after $(( $(date +%s) - T0 ))s"
echo "----- log"
cat /tmp/container.log
EOS
)"
grep -q 'both-ports-up' <<<"$out" || fail "the console and the dashboard did not both come up:" "$out"
grep -q 'config=confs runtime tuner' <<<"$out" || fail "the config volume was not laid out as confs/ runtime/ tuner/:" "$out"
grep -q 'tuner-config=config.json' <<<"$out" || fail "the tuner config was not created in the config volume:" "$out"
grep -q 'runtime-link=/config/runtime' <<<"$out" && grep -q 'confs-link=/config/confs' <<<"$out" \
  || fail "/app/runtime and /app/confs are not linked into the config volume:" "$out"
grep -q 'link=/mnt/media/movies/Film/Film.mkv' <<<"$out" && grep -q 'link2=/mnt/media/anime/Show/e1.mp4' <<<"$out" \
  || fail "sync did not link the videos from both media volumes:" "$out"
grep -q 'pool=./anime/Show/e1.mp4 ./movies/Film/Film.mkv ./movies/Film/Film.nfo ./tanfo/Chan/x.mp4 $' <<<"$out" \
  || fail "the pool is not exactly the videos and the nfo (no poster):" "$out"
grep -q 'catalog-top=by-genre media-links mine->link $' <<<"$out" \
  || fail "the catalog's top level is wrong (by-genre created, old whole-folder links removed, the user's own link kept, pool a real folder):" "$out"
grep -q '^bump=sw43-splash.mp4 $' <<<"$out" && grep -q '^bump-real=1$' <<<"$out" \
  || fail "by-genre/bump was not created with one real generic clip:" "$out"
grep -qE '^bump-owner=([0-9]+:[0-9]+) catalog-owner=\1$' <<<"$out" \
  || fail "the new bump folder does not take the catalog folder's owner:" "$out"
grep -qE 'entrypoint: Station Wrapper 43 [^ ]+ \(commit [^ ,]+, built' <<<"$out" && grep -q 'entrypoint: contains FieldStation42 [0-9a-f]\{7\} and TA_NFOWriter v[0-9]' <<<"$out" \
  || fail "the log does not carry the build and component versions:" "$out"
# and they come last among the entrypoint's lines, right before the supervisor starts
awk '/entrypoint: contains FieldStation42/{f=NR} /^entrypoint: /{l=NR} /\[supervisor\]/{if(!s)s=NR} END{exit !(f && f==l && s>f)}' <<<"$out" \
  || fail "the build lines are not the last entrypoint lines before the supervisor's:" "$out"
grep -q '^init-exit=0$' <<<"$out" && grep -q '^groups-written=yes$' <<<"$out" \
  || fail "fs42-channels init did not run and write /config/groups.toml inside the container:" "$out"
grep -q '^build-exit=0$' <<<"$out" && grep -q '^build-says=dry run: nothing written$' <<<"$out" \
  || fail "fs42-channels build --dry-run did not run inside the container:" "$out"
grep -q '^files-api=True,True,2,True$' <<<"$out" && grep -q '^files-gone=True media-kept=True$' <<<"$out" \
  || fail "the file manager API did not build and remove a list inside the container:" "$out"
grep -q '^test-station=Test Pattern,True$' <<<"$out" && grep -q '^test-flag=yes test-clip=yes$' <<<"$out" \
  && grep -q '^console-schedule=yes$' <<<"$out" \
  && grep -q '^stations-api=Test Pattern:2:True:True$' <<<"$out" && grep -q '^guide-station=1:True:guide:modern-blue$' <<<"$out" && grep -q '^station-manager=ok$' <<<"$out" && grep -q '^schedule-api=1:True$' <<<"$out" \
  && grep -q '^station-form=loop:Test Pattern$' <<<"$out" && grep -q '^pages=200,200,200,200$' <<<"$out" \
  && grep -q '^block-api=1440:test-pattern.mp4:feature$' <<<"$out" \
  && grep -q '^rename-api=404$' <<<"$out" \
  && grep -q '^keeper=True,7,passed$' <<<"$out" \
  && grep -q '^login=401,302,200,setup200,200,401$' <<<"$out" \
  || fail "a new install did not put the guide channel (channel 1) and the test station (channel 2) on air:" "$out"
grep -q '^dashboard=\[' <<<"$out" || fail "the dashboard did not answer with a channel list:" "$out"
grep -q '\[supervisor\] running: console, tuner, firstrun, farm, sync, ta-nfo' <<<"$out" || fail "the supervisor did not start all six:" "$out"
grep -q '\[farm\] farm: .*preparing /app/catalog/by-genre' <<<"$out" && grep -q '\[farm\] farm: added' <<<"$out" \
  || fail "the farm job did not run a pass on its own:" "$out"
grep -q 'ta-nfo: there is no TubeArchivist library at /mnt/media/not-there' <<<"$out" \
  || fail "the missing TubeArchivist library was not reported by the job:" "$out"
grep -q '^ta-error=True,env,error$' <<<"$out" && grep -q '^ta-off=disabled$' <<<"$out" \
  || fail "the TubeArchivist panel API did not show the error and then switch the job off:" "$out"
grep -q 'ta-nfo will restart\|ta-nfo exited' <<<"$out" && fail "the TubeArchivist job died instead of reporting:" "$out"
grep -q 'exit=0 after' <<<"$out" || fail "SIGTERM did not stop the container cleanly:" "$out"
secs="$(sed -n 's/^exit=0 after \([0-9]*\)s/\1/p' <<<"$out")"
[ "${secs:-99}" -le 10 ] || fail "stopping took ${secs}s, longer than Docker's 10s grace:" "$out"
echo "config + catalog + separate media volumes: console, dashboard and sync run together, the TubeArchivist job reports a missing library and is switched off from the dashboard, SIGTERM stops it in ${secs}s: ok"
echo "smoke ok: $image"
