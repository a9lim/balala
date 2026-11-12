# Changelog

## 1.0.0 (2026-11-12)

- On Linux, balala sets Balatro's Steam launch options itself, adding `WINEDLLOVERRIDES="winmm=n,b"` to whatever was
  there. It waits until Steam is closed, since Steam rewrites its config on exit, and backs that config up first.
- On PyPI: `uv tool install balala` or `pipx install balala`.

## 0.1.1 (2026-11-12)

- `balala update` also updates balala to its newest release, the way it was installed: `uv tool`, pipx, pip, or the
  single file. A git checkout is left alone.

## 0.1.0 (2026-11-12)

First release.

- `balala` launches Balatro with lovely, running setup the first time.
- `balala setup` installs lovely, Steamodded and Amulet.
- `balala install` takes mod names (found on Thunderstore or GitHub), GitHub repos (`owner/repo`, URLs,
  `/tree/<branch>` links), other git URLs, `.zip`/`.tar.gz` links, and local archives or checkouts, and warns about
  missing dependencies.
- Mods installed by hand are found and taken over: listed, updated, and de-duplicated. Warnings for mods Steamodded
  won't load together.
- `balala update` updates everything, keeping each mod on the channel it was installed with unless you pass
  `-l`/`-s`.
- `balala list`, `balala remove` and `balala logs [-f]`.
- `-l/--latest` (the default) tracks the newest commit; `-s/--stable` uses the newest release. lovely is compiled, so
  it always uses its newest release.
- `update` leaves git clones with uncommitted edits alone.
- macOS, Windows and Linux (Steam + Proton). Stdlib-only Python 3.9+, plus git.
