# balala

[![PyPI](https://img.shields.io/pypi/v/balala.svg)](https://pypi.org/project/balala/)
[![CI](https://github.com/a9lim/balala/actions/workflows/ci.yml/badge.svg)](https://github.com/a9lim/balala/actions/workflows/ci.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](https://github.com/a9lim/balala/blob/main/LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![macOS | Windows | Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey.svg)

One command for modded [Balatro](https://store.steampowered.com/app/2379780/Balatro/).

balala installs [lovely](https://github.com/ethangreen-dev/lovely-injector) and
[Steamodded](https://github.com/Steamodded/smods), plus
[Amulet](https://github.com/frostice482/amulet), the maintained Talisman successor that big mods such as
Cryptid expect. It installs mods from a link, keeps everything updated, and launches the game.

```console
$ balala
First run: setting up lovely, Steamodded and Amulet.
  + lovely  v0.10.0
  + smods   main@6916f1e
  + amulet  main@9c952a2
▶ Balatro is starting with lovely v0.10.0 and 2 mods. Logs: balala logs -f

$ balala install SpectralPack/Cryptid
  + Cryptid  main@cdb0ddc

$ balala update
  ✓ lovely   v0.10.0
  ↑ smods    main@6916f1e → 1a2b3c4
  ✓ amulet   main@9c952a2
  ✓ Cryptid  main@cdb0ddc
```

## Install

You need Balatro from Steam, Python 3.9 or newer, and git.

```sh
uv tool install balala
# or
pipx install balala
```

balala is one file with no dependencies, so you can also download `balala.py` from the
[latest release](https://github.com/a9lim/balala/releases/latest) and run `python3 balala.py`.

`balala update` keeps balala itself current too, through whichever of these you used. A git checkout is left for
you to update.

## Usage

| Command | |
| --- | --- |
| `balala` | Launch Balatro with your mods. The first time, this runs `setup`. |
| `balala setup [-l\|-s]` | Install lovely, Steamodded and Amulet. |
| `balala install MOD... [-l\|-s] [-n NAME]` | Install mods by name or link. Installing a mod that's already there updates it. |
| `balala update [-l\|-s]` | Update lovely, Steamodded, Amulet, every mod (including ones you installed by hand), and balala itself. |
| `balala list` | Show what's installed. |
| `balala remove NAME...` | Remove mods. |
| `balala logs [-f] [-n N]` | Show the game's latest lovely log; `-f` follows it as the game runs. |

`install` takes:

- a mod's name, like `Cryptid`, looked up on [Thunderstore](https://thunderstore.io/c/balatro/) and GitHub;
- a GitHub repo: `SpectralPack/Cryptid`, `https://github.com/SpectralPack/Cryptid`, or a branch link like
  `https://github.com/owner/repo/tree/dev`;
- any other git URL, such as a GitLab or Codeberg repo;
- a `.zip`/`.tar.gz` link, such as a release asset;
- a local archive or git checkout.

Each mod gets its own folder in `Mods/`, named after the repo. `-n` picks another name. After an install, balala
reads the mod's Steamodded metadata and warns about dependencies that aren't installed. After any change, it warns
about mods Steamodded won't load together: the same mod twice, or a mod next to one that replaces it, like Talisman
next to Amulet.

### Mods you installed by hand

balala takes over mods that were already in your Mods folder, so `list` shows them and `update` keeps them current.
It works out where each one came from:

- a git clone, from its remote;
- Steamodded, from its layout;
- anything else, from the mod's own metadata (such as a Thunderstore manifest's link), Thunderstore's Balatro
  catalog, or a GitHub search for the mod's id. A match counts only if that repo declares the same Steamodded id, so
  a different mod with the same name is never mistaken for yours.

A hand install that looks like a release (say, a `Cryptid-0.5.17d` folder) stays on releases; anything else tracks
the newest commit. The next `update` replaces it with a fresh copy from its source. If you have two copies of one
mod, which Steamodded won't load, balala keeps one and deletes the other. Folders it can't place, including mods
with no Steamodded header, are listed and left alone.

### Channels

- **`-l` / `--latest`** (the default for `setup` and `install`) tracks the newest commit on the default branch,
  or on the branch you linked, as a shallow git clone.
- **`-s` / `--stable`** installs the newest published release: the release's zip if it has one, otherwise the
  tag's source. A repo with no releases tracks its newest commit until it publishes one.

`balala update` keeps each mod on the channel it was installed with. `update -l` or `update -s` moves everything
to that channel. lovely, which is compiled, and balala itself always move to their newest release.

## Platforms

| | macOS | Windows | Linux |
| --- | --- | --- | --- |
| **Game** | Steam | Steam | Steam, through Proton (including Steam Deck desktop mode) |
| **lovely goes in** | the game folder: `liblovely.dylib` and `run_lovely_macos.sh` | the game folder: `winmm.dll` | the game folder: `winmm.dll` |
| **Mods folder** | `~/Library/Application Support/Balatro/Mods` | `%AppData%\Balatro\Mods` | `…/steamapps/compatdata/2379780/pfx/drive_c/users/steamuser/AppData/Roaming/Balatro/Mods` |
| **Launch** | lovely's script; Steam can't load lovely on macOS | through Steam | through Steam |

balala finds the game through Steam's library list, including extra libraries on other drives.

**Linux:** launch Balatro once from Steam before using balala, so Proton creates its prefix. For lovely to load
under Proton, Balatro's Steam launch options need `WINEDLLOVERRIDES="winmm=n,b" %command%`. balala adds that itself,
keeping any options you already have, and backs up Steam's config (`localconfig.vdf`) first. Steam rewrites that
file when it exits, so balala only edits it while Steam is closed and otherwise asks you to close Steam.
(Balatro has no Linux build on Steam, so Steam always runs it through Proton.)

macOS has been tested end to end with the real game. On Windows and Linux, CI runs install, update and removal
with real mods, and the Steam config edit is tested against sample configs, but nobody has launched the real game
there yet. Reports are welcome.

## What it touches

- lovely's files in the game folder, and your Mods folder. Nothing else in the game.
- Its own state, under `~/Library/Application Support/balala` on macOS, `%LocalAppData%\balala` on Windows, and
  `~/.local/share/balala` on Linux.

A mod's folder belongs to balala: `update` makes it match its source, and `remove` deletes it. The one exception
is a git clone with uncommitted edits, which `update` leaves alone. Local commits don't stop an update, but git
keeps them in `git reflog` for a while.

Remove Steamodded or Amulet and they stay removed (`balala setup` brings them back). lovely is the exception: the
game can't load mods without it, so a launch reinstalls it if it's missing, after a Steam reinstall for example.

## Configuration

| Variable | |
| --- | --- |
| `BALALA_GAME_DIR` | the folder holding `Balatro.app` / `Balatro.exe`, if balala can't find it |
| `BALALA_MODS_DIR` | the Mods folder |
| `BALALA_STEAM_DIR` | your Steam folder, if it's somewhere unusual |
| `BALALA_DATA_DIR` | where balala keeps its state |
| `GITHUB_TOKEN` | raises GitHub's limit of 60 API requests an hour (`gh auth token` is used if you're logged in) |
| `NO_COLOR` | turns off colors |

The token is only ever sent to `api.github.com`.

## Development

```sh
uv run --group dev pytest                  # everything, including tests against GitHub
uv run --group dev pytest -m "not network" # offline only
uv run --group dev ruff check && uv run --group dev ruff format --check
```

To release, bump `__version__` in `balala.py`, add a `CHANGELOG.md` entry, and push a `vX.Y.Z` tag. CI builds it,
makes the GitHub release, and publishes to PyPI.

## Credits

Built by a9 and Claude.

lovely, Steamodded and Amulet are their authors' work. balala downloads them, and every mod, from upstream and
redistributes nothing. balala isn't affiliated with LocalThunk or Playstack.

Licensed under the [GNU General Public License v3.0 or later](https://github.com/a9lim/balala/blob/main/LICENSE).
