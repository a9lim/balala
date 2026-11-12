# Changelog

## 0.1.0 (2026-11-12)

First release.

- `balala` launches Balatro with lovely, running setup the first time.
- `balala setup` installs lovely, Steamodded and Amulet.
- `balala install` takes GitHub repos (`owner/repo`, URLs, `/tree/<branch>` links), other git URLs, `.zip`/`.tar.gz`
  links, and local archives or checkouts, and warns about missing dependencies.
- `balala update` updates everything, keeping each mod on the channel it was installed with unless you pass
  `-l`/`-s`.
- `balala list`, `balala remove` and `balala logs [-f]`.
- `-l/--latest` (the default) tracks the newest commit; `-s/--stable` uses the newest release. lovely is compiled, so
  it always uses its newest release.
- macOS, Windows and Linux (Steam + Proton). Stdlib-only Python 3.9+, plus git.
