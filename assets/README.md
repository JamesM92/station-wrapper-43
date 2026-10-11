# Pre-rendered clips

Made once and kept in the repo, so a new install has them without rendering anything (`ffmpeg` with Pillow for the
splash card). The image copies this folder to `/opt/assets/`.

| File | What it is | Used for |
|---|---|---|
| `sw43-splash.mp4` | the Station Wrapper 43 splash card, 5 seconds, 1280x720, silent | the default bump clip (`catalog/by-genre/bump/`) when there are no bump clips or commercials of your own |
| `test-pattern.mp4` | colour bars and a quiet 1 kHz tone, 60 seconds, 1280x720 | the time that no slot is assigned (off air), and the Test Pattern station |

To change one, replace the file and rebuild the image. Keep them small: they are copied into every image.
