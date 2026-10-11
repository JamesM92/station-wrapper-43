# Releasing

How a release is made, and what to do the day this moves to GitHub.

## The gated sequence

1. Work lands on `dev`, with its entry under *Unreleased* in `CHANGELOG.md`. CI (lint, tests, the security scans) runs on each push.
2. To release: on `dev` flip *Unreleased* to the version and date in `CHANGELOG.md`, and commit `vX.Y.Z: <one line>`.
3. Open a pull request `dev` -> `main`. Every workflow that targets `main` runs on the merge commit. Wait for green.
4. Merge with `--rebase` (a linear history). Then, on `main`: `git tag vX.Y.Z`, push the tag; the `release-image` workflow smoke-tests
   the image and publishes `:X.Y.Z` and `:latest`. Create the release notes from the changelog entry.

## Before the first public release (0.5.0)

- [ ] Everything under *Unreleased* in the changelog is true, and the version heading is dated.
- [x] `scripts/e2e-breaks.py` and `scripts/e2e-seasons.py` pass against an image built from `dev` (14 of 14 and 7 of 7, 2026-10-11; both need port 5004 free).
- [ ] A fresh install on an empty config volume reaches **Make me a channel now** -> a channel on air, with the image pulled from the
      public registry (no login). Done 2026-10-11 with the `:dev` image on an empty volume (the guide and test pattern appear, quickstart
      makes a channel, it streams h264/aac and survives a restart); still to do: from the public registry without a login, after the move.
- [x] An upgrade from the version in use here starts and migrates (the passthrough setting, the schedules file). Done by the owner on the real
      install, 2026-10-11, including the container, image and volume renames.
- [ ] The security workflow is green on the PR to `main`, and nothing in `.trivyignore` is out of date.
- [ ] `NOTICE` lists everything the image installs or downloads (check the Dockerfile).

## Moving to GitHub

- [ ] Create the repository (`JamesM92/station-wrapper-43`), push `main`, `dev` and the tags.
- [ ] The workflows for GitHub are in `.github/workflows/` (the same checks, CodeQL, and image builds to GHCR using the built-in
      token). 
- [ ] Image name: `ghcr.io/jamesm92/station-wrapper-43`. Update `docker-compose.yml` (`image:`), README's Deploy section (no `docker login`
      for a public package), and `FS42_TAG`'s default.
- [ ] Make the package public in GitHub's package settings.
- [ ] Turn on Dependabot alerts and secret scanning, and protect `main` (require the CI and security checks).
- [ ] Update the links: README, NOTICE, `LICENSE`'s "Required Notice" line, and the URLs in `scripts/` and the workflows.
