# balala

Modded Balatro on macOS (Steam), in one command. `balala` installs
[lovely](https://github.com/ethangreen-dev/lovely-injector),
[Steamodded](https://github.com/Steamodded/smods) and
[Amulet](https://github.com/frostice482/amulet), installs mods from a link,
keeps them all updated, and launches the game through lovely.

```sh
balala                                            # launch (runs setup on first use)
balala install https://github.com/SpectralPack/Cryptid
balala update
```

## Install

It's one stdlib-only Python file (any `python3` ≥ 3.9, plus `git`). Link it onto
your PATH:

```sh
ln -sfn ~/Work/balala/balala ~/.local/bin/balala
```

## Commands

| Command | What it does |
| --- | --- |
| `balala` / `balala launch [-- args]` | Launch Balatro with lovely via the stock `run_lovely_macos.sh`. Runs `setup` first if it never has. |
| `balala setup [-s\|-l]` | Install lovely, Steamodded and Amulet. |
| `balala install URL... [-s\|-l] [-n NAME]` | Install mods. Takes a GitHub repo (`https://github.com/o/r`, `github.com/o/r`, `o/r`, or `…/tree/branch`), any git URL, or a direct `.zip`/`.tar.gz` link. Re-running it on an installed mod updates it or switches its channel. |
| `balala update [-s\|-l]` | Update lovely, Steamodded, Amulet and every mod balala installed. |
| `balala list` | Show what's installed: channel, version or commit, source. Folders balala didn't install are listed as `unmanaged`. |
| `balala remove NAME...` | Remove mods. |

### Channels

- `-l` / `--latest` (the default) tracks HEAD of the default branch (or the
  `/tree/` branch you linked) as a shallow git clone. `update` fast-forwards it.
- `-s` / `--stable` installs the newest published release: the release's own
  `.zip` asset if it has exactly one, otherwise the tag's source archive. A repo
  with no releases falls back to HEAD, with a warning.
- Anything that needs compiling is release-only, whatever the flag. Of the things
  balala installs, only lovely (Rust) needs it.

The flag applies to everything a command touches. So plain `balala update`
moves stable installs to HEAD, and `balala update -s` pins everything to
releases.

## Where things go

| What | Where |
| --- | --- |
| lovely (`liblovely.dylib`, `run_lovely_macos.sh`) | the game folder, found through Steam's `libraryfolders.vdf` |
| mods | `~/Library/Application Support/Balatro/Mods/<repo name>` |
| balala's record of what it installed | `~/Library/Application Support/balala/state.json` |
| anything balala had to move out of the way | `~/Library/Application Support/balala/backups/` |

Overrides: `BALALA_GAME_DIR` (the folder holding `Balatro.app`) and `BALALA_MODS_DIR`.

## Behavior worth knowing

- **Local work is never discarded.** `update` fast-forwards clones and leaves a
  clone alone if it has uncommitted edits or local commits. When upstream rewrites
  history, a clone balala hasn't touched since it put it there is reset to match.
  Replacing a folder that might hold your work (an unmanaged folder, or a clone
  with edits) moves it to `backups/` instead of deleting it.
- **Removed core mods stay removed.** After the first setup, a bare `balala`
  doesn't bring back a Steamodded or Amulet you removed (say, to try Talisman
  instead). `balala setup` does. The one exception is lovely: the game can't
  launch without it, so `balala` reinstalls it if it's missing (after a Steam
  reinstall, say).
- **Dependency hints.** After `install`, balala reads the mod's Steamodded
  header and warns about any `dependencies` it can't find among installed mods,
  counting what other mods `provide`. Amulet provides `Talisman`, for example.
- **GitHub API.** balala uses `GITHUB_TOKEN`/`GH_TOKEN` or `gh auth token` when
  available. The token goes only to `api.github.com`, never to download hosts.
  Without one you get GitHub's anonymous limit of 60 requests an hour.

## Credits

Built by a9 and Claude. lovely, Steamodded and Amulet are their authors' work;
balala only fetches them.
