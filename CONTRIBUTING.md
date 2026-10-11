# Contributing

Thanks for looking. This is a wrapper around [FieldStation42](https://github.com/shane-mason/FieldStation42): it runs it unmodified in
one container and adds a dashboard, a tuner (HDHomeRun emulation, guide, player) and the pages for building stations.

- **Layout:** `fs42-tuner/` is the dashboard and tuner; `fieldstation42/` holds the vendored upstream (never edited by hand: update
  with `scripts/sync-fs42.sh`) and `console_launch.py`, the few run-time adjustments; `scripts/` the generators and checks;
  `supervisor/`, `fs42-sync/`, `ta-nfo/`, `farm/` the container's other services.
- **Tests:** every module has a `test_*.py` beside it; CI runs them all (`ci.yml`). `scripts/e2e-*.py` run against a real image.
- **Style:** match the code around you. Comments say why. A change that a person sees gets a line in `CHANGELOG.md` under *Unreleased*.
- **Branches:** work on `dev`; `main` moves only through a pull request that has passed CI and the security checks.
- **Security problems:** see [SECURITY.md](SECURITY.md), not a public issue.
- **License:** the wrapper's code is PolyForm Noncommercial 1.0.0 (see `LICENSE`); contributions are under the same terms.
