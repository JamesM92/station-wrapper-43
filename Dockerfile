# syntax=docker/dockerfile:1
# ONE image, one container. It runs, under a small supervisor (supervisor/supervisor.py):
#   console   FieldStation42's web console and scheduler (station_42.py), on :4242
#   tuner     the dashboard, player and video streams (fs42-tuner), on :5004
#   sync      keeps the symlink pool current                (SYNC_ENABLED=1)
#   ta-nfo    builds the TubeArchivist view with TA_NFOWriter (TA_ENABLED=1)
#
# THREE volumes make it work:
#   /config        all configuration: runtime/ (schedule database), confs/ (stations),
#                  tuner/ (the dashboard's config), groups.toml. LOCAL disk.
#   /app/catalog   everything made of symlinks: by-genre/ (tag folders), tanfo/ (the
#                  TubeArchivist view), media-links/ (a link for every video). LOCAL disk.
#   /mnt/media     your media, read-only. It may hold several separate shares as subfolders.
#
# Headless build of FieldStation42 (https://github.com/shane-mason/FieldStation42): only
# station_42.py is run. field_player.py (the mpv/X11 on-screen player) is never started, so
# none of its dependencies (mpv, X11 libs, pulseaudio) are installed. FieldStation42's own guide
# channel needs tkinter and a screen, so the tuner draws its own (fs42-tuner/guide_channel.py, with Pillow). The pip list below is hand-picked from
# FieldStation42's install/requirements.in rather than installed wholesale, which would pull
# mpv/GL/Qt bindings that only field_player uses.
FROM python:3.11-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Some Docker hosts hand out an IPv6 address on the bridge network with no actual route;
# apt/pip then try the AAAA record first and stall for ~30s per host. Telling glibc's
# resolver to sort A records first sidesteps that everywhere at once.
RUN echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf

# libtk8.6: FieldStation42's console checks a guide station by importing its Tk guide module, so Python's _tkinter
# must load (nothing is ever drawn: no display, no Tk window). Without it the console will not start at all once a
# guide station exists.
# ffmpeg: FieldStation42 shells out to ffprobe, and the tuner encodes streams with it.
# tini: PID 1, so processes that outlive their parent (ffmpeg) are reaped.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
      libtk8.6 \
      tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# FieldStation42: vendored, unmodified. Updated with scripts/sync-fs42.sh, not by hand.
COPY fieldstation42/src/ /app/
# FieldStation42 reads runtime/ and confs/ relative to /app. They live in ONE config volume
# (/config), and the entrypoint links /app/runtime and /app/confs into it. The sample confs
# the source ships are dropped so that link can take their place (a volume used to hide them).
# /config and /app/catalog are deliberately NOT created here: when a volume is first used, Docker
# copies the ownership of an existing image folder at the mount path onto the volume's folder on
# the host. That would turn the folders you made as yourself into root's, and the scripts you run
# on the host (init.py, build_channels.py, fs42_link.py) could no longer write to them.
# The two links are made here (dangling until the config volume is mounted) so a container started as a user that
# cannot write to /app has them too.
RUN rm -rf /app/confs /app/runtime /app/catalog \
    && ln -s /config/runtime /app/runtime && ln -s /config/confs /app/confs
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# The tuner. The player script is fetched at build time and checked against a SHA-256, so
# a changed file fails the build. To update: change the version and the hash together.
COPY fs42-tuner/resolver.py fs42-tuner/tuner.py fs42-tuner/guide.py \
     fs42-tuner/upstream.py fs42-tuner/library.py fs42-tuner/stations.py fs42-tuner/files.py fs42-tuner/settings.py fs42-tuner/tubearchivist.py fs42-tuner/console_client.py fs42-tuner/stationform.py fs42-tuner/stationsapi.py fs42-tuner/keeper.py fs42-tuner/schedlock.py fs42-tuner/auth.py fs42-tuner/captions.py fs42-tuner/legacy.py fs42-tuner/schedules.py fs42-tuner/prefetch.py fs42-tuner/guide_channel.py fs42-tuner/breaksnap.py fs42-tuner/breakstore.py fs42-tuner/iptv.py fs42-tuner/titles.py fs42-tuner/diag.py fs42-tuner/channel_logo.py fs42-tuner/art.py fs42-tuner/ccinject.py fs42-tuner/loudness.py fs42-tuner/webget.py fs42-tuner/rundir.py fs42-tuner/premieres.py fs42-tuner/midcards.py fs42-tuner/streaming.py fs42-tuner/ordering.py /opt/tuner/
COPY fs42-tuner/static/ /opt/tuner/static/
ADD --checksum=sha256:ceb67d511fae54f442a5b9c03e6b8ea30a89efc9bde7fa3ce0687f05f31e6d6a \
    https://cdn.jsdelivr.net/npm/mpegts.js@1.8.2/dist/mpegts.min.js /opt/tuner/static/mpegts.min.js
COPY fs42-tuner/config.default.json /opt/tuner-config.default.json

# The symlink pool scanner and catalog watcher.
COPY fs42-sync/pool.py fs42-sync/sync.py fs42-tuner/schedlock.py fs42-tuner/console_client.py fs42-tuner/breaksnap.py fs42-tuner/breakstore.py fs42-tuner/webget.py fs42-tuner/rundir.py /opt/sync/

# TA_NFOWriter (https://github.com/JamesM92/TA_NFOWriter): not copied into this repo. One
# pinned release tag, verified by SHA-256. To update: change both ARGs together.
ARG TA_NFOWRITER_REF=v0.5.0
ARG TA_NFOWRITER_SHA256=068fdfc9ec8c30b9efe19b9f2e7f5d9452b174a99909e131cecf0ebb73f0b4bc
ADD --checksum=sha256:${TA_NFOWRITER_SHA256} \
    https://raw.githubusercontent.com/JamesM92/TA_NFOWriter/${TA_NFOWRITER_REF}/ta_nfo.py /opt/ta-nfo/ta_nfo.py
COPY ta-nfo/service.py fs42-tuner/settings.py fs42-tuner/rundir.py /opt/ta-nfo/

# The channel tools (init / build / link) run in here too, so no clone or pip is needed on the host:
#   docker exec <container> fs42-channels build --dry-run
COPY scripts/init.py scripts/build_channels.py scripts/fs42_link.py scripts/firstrun.py scripts/update_catalog.py fs42-tuner/schedlock.py fs42-tuner/webget.py fs42-tuner/rundir.py fs42-tuner/ordering.py fieldstation42/console_launch.py /opt/wrapper/
COPY fieldstation42/fs42-channels /usr/local/bin/fs42-channels
COPY assets/ /opt/assets/
COPY farm/run.sh /opt/farm/run.sh
COPY supervisor/supervisor.py fs42-tuner/stations.py fs42-tuner/rundir.py /opt/supervisor/
COPY fieldstation42/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh /opt/farm/run.sh /usr/local/bin/fs42-channels && chmod 644 /opt/ta-nfo/ta_nfo.py /opt/tuner/static/mpegts.min.js

ENV FS42_TUNER_CONF=/config/tuner/config.json \
    FS42_IN_CONTAINER=1

# Build information, printed first in the log and kept as image labels. CI passes the real values
# (dev-image.yml, release-image.yml); a plain `docker build` leaves "dev" and "unknown". Last, so a
# new commit rebuilds only this cheap layer. The FieldStation42 commit comes from the pin file.
ARG BUILD_REF=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown
COPY fieldstation42/FS42_REF /opt/FS42_REF
# Our licence travels with the image (FieldStation42's own, MPL-2.0, is /app/LICENSE).
COPY LICENSE /usr/share/doc/station-wrapper-43/LICENSE
ENV FS42_WRAPPER_REF=${BUILD_REF} \
    FS42_WRAPPER_SHA=${BUILD_SHA} \
    FS42_WRAPPER_BUILT=${BUILD_DATE} \
    TA_NFOWRITER_VERSION=${TA_NFOWRITER_REF}
LABEL org.opencontainers.image.title="Station Wrapper 43" \
      org.opencontainers.image.version="${BUILD_REF}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0 AND MPL-2.0"

# 4242: FieldStation42's console. 5004: the dashboard and player (the port a real
# HDHomeRun serves on, so clients that probe for one find this where they expect it).
EXPOSE 4242 5004
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
