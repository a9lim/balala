# balala

[![CI](https://github.com/a9lim/balala/actions/workflows/ci.yml/badge.svg)](https://github.com/a9lim/balala/actions/workflows/ci.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
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
uv tool install git+https://github.com/a9lim/balala
# or
pipx install git+https://github.com/a9lim/balala
```

balala is one file with no dependencies, so you can also download `balala.py` from the
[latest release](https://github.com/a9lim/balala/releases/latest) and run `python3 balala.py`.

## Usage

| Command | |
| --- | --- |
| `balala` | Launch Balatro with your mods. The first time, this runs `setup`. |
| `balala setup [-l\|-s]` | Install lovely, Steamodded and Amulet. |
| `balala install LINK... [-l\|-s] [-n NAME]` | Install mods. Installing a mod that's already there updates it. |
| `balala update [-l\|-s]` | Update lovely, Steamodded, Amulet and every mod balala installed. |
| `balala list` | Show what's installed, and any mod folders balala doesn't manage. |
| `balala remove NAME...` | Remove mods. |
| `balala logs [-f] [-n N]` | Show the game's latest lovely log; `-f` follows it as the game runs. |

`install` takes:

- a GitHub repo: `SpectralPack/Cryptid`, `https://github.com/SpectralPack/Cryptid`, or a branch link like
  `https://github.com/owner/repo/tree/dev`;
- any other git URL, such as a GitLab or Codeberg repo;
- a `.zip`/`.tar.gz` link, such as a release asset;
- a local archive or git checkout.

Each mod gets its own folder in `Mods/`, named after the repo. `-n` picks another name. After an install, balala
reads the mod's Steamodded metadata and warns about dependencies that aren't installed.

### Channels

- **`-l` / `--latest`** (the default for `setup` and `install`) tracks the newest commit on the default branch,
  or on the branch you linked, as a shallow git clone.
- **`-s` / `--stable`** installs the newest published release: the release's zip if it has one, otherwise the
  tag's source. A repo with no releases tracks its newest commit until it publishes one.

`balala update` keeps each mod on the channel it was installed with. `update -l` or `update -s` moves everything
to that channel. lovely is compiled, so it always uses its newest release.

## Platforms

| | macOS | Windows | Linux |
| --- | --- | --- | --- |
| **Game** | Steam | Steam | Steam, through Proton (including Steam Deck desktop mode) |
| **lovely goes in** | the game folder: `liblovely.dylib` and `run_lovely_macos.sh` | the game folder: `winmm.dll` | the game folder: `winmm.dll` |
| **Mods folder** | `~/Library/Application Support/Balatro/Mods` | `%AppData%\Balatro\Mods` | `…/steamapps/compatdata/2379780/pfx/drive_c/users/steamuser/AppData/Roaming/Balatro/Mods` |
| **Launch** | lovely's script; Steam can't load lovely on macOS | through Steam | through Steam |

balala finds the game through Steam's library list, including extra libraries on other drives.

**Linux** needs one thing done by hand, because Steam doesn't let other programs change its settings. Set
Balatro's launch options to `WINEDLLOVERRIDES="winmm=n,b" %command%`, and launch the game once from Steam before
using balala so Proton creates its prefix. balala reminds you until the option is set.

macOS has been tested end to end with the real game. On Windows and Linux, CI runs install, update and removal
with real mods, but the launch step hasn't been run against the real game yet. Reports are welcome.

## What it touches

- lovely's files in the game folder, and your Mods folder. Nothing else in the game.
- Its own state, under `~/Library/Application Support/balala` on macOS, `%LocalAppData%\balala` on Windows, and
  `~/.local/share/balala` on Linux.

It's careful with your work:

- `update` only fast-forwards a git clone, and leaves any clone with uncommitted edits or local commits alone. If
  upstream rewrites its history, a clone balala hasn't touched since it put it there is reset to match.
- balala only deletes a mod folder it can prove is untouched: a clean clone with no new, ignored or committed
  files of yours, or a release or archive install whose files still match what was unpacked. Anything else that has
  to go, including any folder balala didn't install, is moved to a `backups` folder next to its state.

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

To release, bump `__version__` in `balala.py`, add a `CHANGELOG.md` entry, and push a `vX.Y.Z` tag. CI builds the
release.

## Credits

Built by a9 and Claude.

lovely, Steamodded and Amulet are their authors' work. balala downloads them, and every mod, from upstream and
redistributes nothing. balala isn't affiliated with LocalThunk or Playstack.

Licensed under the [GNU General Public License v3.0 or later](LICENSE).
