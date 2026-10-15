# Station Wrapper 43: quickstart

1. **Start it.** Use the compose file in `README.md`: three volumes (`/config`, `/app/catalog`, `/mnt/media`) and port 5004. The first start sets up a Channel Guide on channel 1 and a Test Pattern on channel 2.
2. **Open the dashboard** at `http://<host>:5004`. The **Status** page tells you at once if something is wrong.
3. **Put videos in the media folder**, then use the **Files** page to build lists and the **Stations** page to make a channel with a schedule.
4. **Add it to Jellyfin** (or any IPTV client): one tuner of type HDHomeRun at `<host>:5004`, and one XMLTV guide at `http://<host>:5004/xmltv.xml`. Then run Refresh Guide. (TVHeadend: use `http://<host>:5004/lineup.m3u`.)
5. **Add an antenna tuner, a playlist or a camera** on the **Sources** page. They go through this container too, so the client only ever talks to it.
6. **Something wrong?** Open **Status**, press **Download diagnostics**, and send the file.

The services run as `PUID`:`PGID` (unset: whoever owns the config folder, so run `setup-dirs.sh` as the account that should own your
files; set both in `.env` to use another one, and make sure it can read your media).

Back up the config folder; upgrading is `docker compose pull && docker compose up -d`. More detail is in the [reference](docs/REFERENCE.md).
