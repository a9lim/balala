#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 a9lim and Claude
"""balala: one command for modded Balatro.

Installs lovely, Steamodded and Amulet; installs and updates mods from a link;
launches the game. Works on macOS, Windows and Linux (Steam + Proton) with
nothing but the Python standard library (3.9+) and git.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.1.0"

APP_ID = "2379780"  # Balatro's Steam app id
LOVELY_REPO = "ethangreen-dev/lovely-injector"
CORE = {"smods": "Steamodded/smods", "amulet": "frostice482/amulet"}  # Mods/ folder -> repo
LOVELY_DLLS = ("winmm.dll", "version.dll")  # names lovely has shipped under on Windows
ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz")


class Fail(Exception):
    """An expected failure, shown to the user as a one-line error."""


def die(msg: str):
    raise Fail(msg)


# ── output ────────────────────────────────────────────────────────────────

_COLOR = False


def _enable_vt() -> bool:
    """ANSI colors work everywhere except a legacy Windows console, where we turn them on."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ok = False
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ok = bool(kernel32.SetConsoleMode(handle, mode.value | 0x4)) or ok
        return ok
    except Exception:
        return False


def paint(text: str, code: str, stream=None) -> str:
    stream = stream or sys.stdout
    return f"\033[{code}m{text}\033[0m" if _COLOR and stream.isatty() else text


def warn(msg: str) -> None:
    print(f"{paint('!', '1;33', sys.stderr)} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{paint('error:', '1;31', sys.stderr)} {msg}", file=sys.stderr)


def tilde(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home) :] if s == home or s.startswith(home + os.sep) else s


def os_error(e: OSError) -> str:
    where = f": {tilde(Path(e.filename))}" if e.filename else ""
    return f"{e.strerror or e}{where}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@dataclass
class Result:
    status: str  # current | installed | updated | kept | removed | failed
    detail: str = ""


GLYPHS = {
    "current": ("✓", "2"),
    "installed": ("+", "1;32"),
    "updated": ("↑", "1;32"),
    "kept": ("!", "1;33"),
    "removed": ("−", "1;31"),
    "failed": ("✗", "1;31"),
}

_notes: list[str] | None = None


def note(msg: str) -> None:
    """Attach a remark to the job that's running, or print it as a warning."""
    if _notes is not None:
        _notes.append(msg)
    else:
        warn(msg)


def run_jobs(jobs: list[tuple[str, Callable[[], Result]]]) -> int:
    """Run jobs one line each, carrying on past failures; returns how many failed."""
    global _notes
    width = max((len(label) for label, _ in jobs), default=0)
    live = _COLOR and sys.stdout.isatty()
    failed = 0
    for label, fn in jobs:
        if live:
            print(f"  {paint('·', '2')} {label:<{width}}  {paint('…', '2')}", end="", flush=True)
        _notes = []
        try:
            res = fn()
        except Fail as e:
            res = Result("failed", str(e))
            failed += 1
        except OSError as e:  # a file held open by the game or an editor, a full disk...
            res = Result("failed", os_error(e))
            failed += 1
        finally:
            notes, _notes = _notes, None
        glyph, code = GLYPHS[res.status]
        detail = paint(res.detail, "2") if res.status == "current" else res.detail
        if live:
            print("\r\033[K", end="")
        print(f"  {paint(glyph, code)} {label:<{width}}  {detail}".rstrip())
        for n in notes:
            print(f"  {' ' * (width + 4)}{paint(n, '33')}")
    return failed


# ── platforms ─────────────────────────────────────────────────────────────


@dataclass
class Game:
    dir: Path  # the folder holding Balatro.app / Balatro.exe
    library: Path | None  # the Steam library it's in, when known


def _pgrep(pattern: str) -> bool:
    if not shutil.which("pgrep"):
        return False
    return (
        subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    )


class Platform:
    name = ""
    marker = ""  # what identifies the game folder
    git_hint = "install git"

    def steam_roots(self) -> list[Path]:
        return []

    def mods_dir(self, env: Env) -> Path:
        raise NotImplementedError

    def data_dir(self) -> Path:
        raise NotImplementedError

    def cache_dir(self) -> Path:
        return self.data_dir() / "cache"

    def lovely_asset(self) -> str:
        return "lovely-x86_64-pc-windows-msvc.zip"

    def lovely_default_files(self) -> list[str]:
        return ["winmm.dll"]

    def lovely_files(self, unpacked: Path) -> list[Path]:
        return sorted(unpacked.rglob("*.dll")) or die("lovely's archive has no DLL in it")

    def running(self) -> bool:
        return False

    def launch(self, game: Game) -> None:
        raise NotImplementedError

    def hints(self, game: Game, lovely_files: list[str]) -> list[str]:
        """Setup the user still has to do by hand."""
        return []


class MacOS(Platform):
    name = "macOS"
    marker = "Balatro.app"
    git_hint = "run `xcode-select --install`"

    def steam_roots(self):
        return [Path.home() / "Library/Application Support/Steam"]

    def mods_dir(self, env):
        return Path.home() / "Library/Application Support/Balatro/Mods"

    def data_dir(self):
        return Path.home() / "Library/Application Support/balala"

    def cache_dir(self):
        return Path.home() / "Library/Caches/balala"

    def lovely_asset(self):
        # By hardware, not by this interpreter, which might be running under Rosetta.
        r = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True)
        arch = "aarch64" if r.stdout.strip() == "1" else "x86_64"
        return f"lovely-{arch}-apple-darwin.tar.gz"

    def lovely_default_files(self):
        return ["liblovely.dylib", "run_lovely_macos.sh"]

    def lovely_files(self, unpacked):
        found = [next(unpacked.rglob(n), None) for n in self.lovely_default_files()]
        if None in found:
            die("lovely's archive is missing liblovely.dylib or run_lovely_macos.sh")
        return found

    def running(self):
        return _pgrep("Balatro.app/Contents/MacOS/love")

    def launch(self, game):
        # Steam can't launch with lovely on macOS; lovely's own script (or its
        # equivalent, when the game isn't where the script expects) is the way in.
        script = game.dir / "run_lovely_macos.sh"
        stock = Path("/Users", os.environ.get("USER", ""), "Library/Application Support/Steam/steamapps/common/Balatro")
        if script.is_file() and stock.is_dir() and stock.resolve() == game.dir.resolve():
            cmd, env = ["/bin/bash", str(script)], None
        else:
            love = game.dir / "Balatro.app/Contents/MacOS/love"
            cmd, env = [str(love)], {**os.environ, "DYLD_INSERT_LIBRARIES": "liblovely.dylib"}
        spawn_detached(cmd, game.dir, env)


class Windows(Platform):
    name = "Windows"
    marker = "Balatro.exe"
    git_hint = "install Git for Windows from https://git-scm.com"

    def steam_roots(self):
        roots = []
        try:
            import winreg
        except ImportError:
            winreg = None
        if winreg:
            for hive, key, value in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        roots.append(Path(winreg.QueryValueEx(k, value)[0]))
                except OSError:
                    pass
        for var, default in (("ProgramFiles(x86)", r"C:\Program Files (x86)"), ("ProgramFiles", r"C:\Program Files")):
            roots.append(Path(os.environ.get(var, default)) / "Steam")
        return roots

    def mods_dir(self, env):
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Balatro" / "Mods"

    def data_dir(self):
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "balala"

    def running(self):
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Balatro.exe", "/NH"], capture_output=True, text=True)
        return "balatro.exe" in r.stdout.lower()

    def launch(self, game):
        os.startfile(f"steam://rungameid/{APP_ID}")  # lovely's DLL is picked up from the game folder


class Linux(Platform):
    """Balatro has no native Linux build, so this is the Windows game under Proton."""

    name = "Linux"
    marker = "Balatro.exe"
    git_hint = "install git with your package manager"

    def steam_roots(self):
        home = Path.home()
        flatpak = home / ".var/app/com.valvesoftware.Steam"
        return [
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".steam/root",
            flatpak / ".local/share/Steam",
            flatpak / ".steam/steam",
            home / "snap/steam/common/.local/share/Steam",
        ]

    def mods_dir(self, env):
        game = env.game
        if game.library is None:
            die("can't tell which Steam library Balatro is in; set BALALA_MODS_DIR")
        prefix = game.library / "steamapps/compatdata" / APP_ID / "pfx"
        if not prefix.is_dir():
            die("Proton hasn't set Balatro up yet: launch it once from Steam, then try again")
        return prefix / "drive_c/users/steamuser/AppData/Roaming/Balatro/Mods"

    def data_dir(self):
        return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "balala"

    def cache_dir(self):
        return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "balala"

    def running(self):
        return _pgrep("Balatro.exe")

    def launch(self, game):
        url = f"steam://rungameid/{APP_ID}"
        exe = shutil.which("steam") or shutil.which("xdg-open") or die("can't find steam or xdg-open")
        subprocess.Popen(
            [exe, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def hints(self, game, lovely_files):
        stems = [Path(f).stem.lower() for f in lovely_files if f.lower().endswith(".dll")]
        if not stems:
            return []
        for root in existing(steam_roots(self)):
            for cfg in _localconfigs(root):
                try:
                    data = parse_vdf(cfg.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                opts = dig(data, "userlocalconfigstore", "software", "valve", "steam", "apps", APP_ID, "launchoptions")
                if isinstance(opts, str) and loads_native(opts, stems):
                    return []
        overrides = ";".join(f"{s}=n,b" for s in stems)
        return [f'in Steam, set Balatro\'s launch options to: WINEDLLOVERRIDES="{overrides}" %command%']


def _localconfigs(steam: Path) -> list[Path]:
    """The signed-in Steam account's localconfig.vdf, or every account's if that's unclear."""
    try:
        users = dig(parse_vdf((steam / "config/loginusers.vdf").read_text(encoding="utf-8", errors="replace")), "users")
    except OSError:
        users = None
    for steam_id, user in (users or {}).items():
        if isinstance(user, dict) and user.get("mostrecent") == "1" and steam_id.isdigit():
            cfg = steam / "userdata" / str(int(steam_id) - 76561197960265728) / "config/localconfig.vdf"
            if cfg.is_file():
                return [cfg]
    return sorted(steam.glob("userdata/*/config/localconfig.vdf"))


def loads_native(launch_options: str, dlls: list[str]) -> bool:
    """Whether Steam launch options make Wine load these DLLs natively (n before b), e.g.
    WINEDLLOVERRIDES="winmm=n,b" %command%."""
    m = re.search(r"WINEDLLOVERRIDES\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", launch_options, re.IGNORECASE)
    if not m or "%command%" not in launch_options.lower():
        return False
    modes = {}
    for entry in m.group(1).strip("\"'").split(";"):
        names, _, mode = entry.partition("=")
        for dll in names.split(","):
            modes[dll.strip().lower()] = mode.strip().lower()
    return all(modes.get(d, "").startswith("n") for d in dlls)


def current_platform() -> Platform:
    if sys.platform == "darwin":
        return MacOS()
    if sys.platform == "win32":
        return Windows()
    if sys.platform.startswith("linux"):
        return Linux()
    die(f"unsupported platform: {sys.platform}")


def spawn_detached(cmd: list[str], cwd: Path, env: dict | None) -> None:
    """Start the game in the background; report it if it dies straight away."""
    ENV.data.mkdir(parents=True, exist_ok=True)
    log = ENV.data / "launch.log"
    with open(log, "wb") as out:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as e:
            die(f"couldn't start {cmd[0]}: {e.strerror}")
    try:
        code = proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        return
    tail = log.read_text(errors="replace").strip().splitlines()[-8:]
    die("\n    ".join([f"Balatro quit right away (exit code {code}):", *tail]))


# ── Steam ─────────────────────────────────────────────────────────────────


def parse_vdf(text: str) -> dict:
    """Valve's KeyValues text format, with keys lowercased."""
    root: dict = {}
    stack = [root]
    key = None
    for quoted, brace in re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text):
        if brace == "{":
            child: dict = {}
            stack[-1][key or ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = quoted.lower()
        else:
            stack[-1][key] = re.sub(r"\\(.)", r"\1", quoted)
            key = None
    return root


def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def steam_roots(plat: Platform) -> list[Path]:
    env = os.environ.get("BALALA_STEAM_DIR")
    return [Path(env).expanduser()] if env else plat.steam_roots()


def existing(paths: list[Path]) -> list[Path]:
    """The paths that exist, without duplicates (~/.steam/steam is usually a symlink)."""
    seen, out = set(), []
    for p in paths:
        try:
            key = p.resolve()
        except OSError:
            continue
        if p.is_dir() and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_game(plat: Platform) -> Game:
    override = os.environ.get("BALALA_GAME_DIR")
    if override:
        d = Path(override).expanduser()
        if not (d / plat.marker).exists():
            die(f"BALALA_GAME_DIR={override} has no {plat.marker} in it")
        is_steam = d.parent.name.lower() == "common" and d.parent.parent.name.lower() == "steamapps"
        return Game(d, d.parents[2] if is_steam else None)
    libraries: list[Path] = []
    for root in existing(steam_roots(plat)):
        libraries.append(root)
        try:
            vdf = parse_vdf((root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for k, entry in (vdf.get("libraryfolders") or {}).items():
            if isinstance(entry, dict) and entry.get("path"):
                path = Path(entry["path"])
                if APP_ID in (entry.get("apps") or {}):
                    libraries.insert(0, path)  # Steam says this one has it
                else:
                    libraries.append(path)
            elif isinstance(entry, str) and k.isdigit():  # pre-2021 format
                libraries.append(Path(entry))
    for lib in existing(libraries):
        d = lib / "steamapps/common/Balatro"
        if (d / plat.marker).exists():
            return Game(d, lib)
    die(
        "couldn't find Balatro in your Steam libraries. If it's installed somewhere unusual, "
        f"set BALALA_GAME_DIR to the folder holding {plat.marker}"
    )


# ── environment & state ───────────────────────────────────────────────────


class Env:
    """Where everything lives, resolved lazily (finding the game can fail)."""

    def __init__(self) -> None:
        self.plat = current_platform()
        data = os.environ.get("BALALA_DATA_DIR")
        self.data = Path(data).expanduser() if data else self.plat.data_dir()
        self.cache = self.data / "cache" if data else self.plat.cache_dir()
        self.state_file = self.data / "state.json"
        self.backups = self.data / "backups"
        self._game: Game | None = None
        self._mods: Path | None = None

    @property
    def game(self) -> Game:
        if self._game is None:
            self._game = find_game(self.plat)
        return self._game

    @property
    def mods(self) -> Path:
        if self._mods is None:
            override = os.environ.get("BALALA_MODS_DIR")
            self._mods = Path(override).expanduser() if override else self.plat.mods_dir(self)
        return self._mods


ENV: Env
STATE: dict = {}


def load_state() -> dict:
    try:
        state = json.loads(ENV.state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except ValueError:
        die(f"{ENV.state_file} is corrupt; fix or delete it")
    state.setdefault("lovely", None)
    state.setdefault("mods", {})
    return state


def save_state() -> None:
    ENV.data.mkdir(parents=True, exist_ok=True)
    tmp = ENV.state_file.with_name(ENV.state_file.name + ".tmp")
    tmp.write_text(json.dumps(STATE, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, ENV.state_file)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def game_running() -> bool:
    return ENV.plat.running()


# ── network ───────────────────────────────────────────────────────────────

_token: str | None = None


def github_token() -> str | None:
    """GITHUB_TOKEN / GH_TOKEN, else `gh auth token`: lifts the 60 requests/hour anonymous limit."""
    global _token
    if _token is None:
        _token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not _token and shutil.which("gh"):
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
            _token = r.stdout.strip() if r.returncode == 0 else ""
    return _token or None


def _open(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": f"balala/{__version__}", **(headers or {})})
    return urllib.request.urlopen(req, timeout=120)


def api(path: str):
    """GET a GitHub API path; None on 404. The token only ever goes to api.github.com."""
    global _token
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with _open(f"https://api.github.com/{path}", headers) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code == 401 and token:
            _token = ""  # a stale token: retry anonymously
            return api(path)
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            die("hit GitHub's rate limit; log in with `gh auth login` or set GITHUB_TOKEN")
        die(f"GitHub said HTTP {e.code} for {path}")
    except urllib.error.URLError as e:
        die(f"can't reach GitHub ({e.reason})")


def download(url: str, dest: Path) -> None:
    try:
        with _open(url) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        die(f"download failed with HTTP {e.code}: {url}")
    except urllib.error.URLError as e:
        die(f"download failed ({e.reason}): {url}")


def newest_release(repo: str) -> dict | None:
    """GitHub's latest release; failing that (a repo with only prereleases), the newest published one."""
    rel = api(f"repos/{repo}/releases/latest")
    if rel:
        return rel
    rels = [r for r in (api(f"repos/{repo}/releases?per_page=20") or []) if not r.get("draft")]
    return rels[0] if rels else None


# ── files ─────────────────────────────────────────────────────────────────


def rmtree(path: Path) -> None:
    def force(func, p, _exc):  # git marks its objects read-only, which Windows won't delete
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


def move(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.copytree(src, dst, symlinks=True)  # across volumes
        rmtree(src)


def fingerprint(folder: Path) -> str:
    """A hash of every file in a folder, to tell whether anyone has changed a mod since it was installed."""
    h = hashlib.sha256()
    for p in sorted((p for p in folder.rglob("*") if p.is_file()), key=lambda p: p.relative_to(folder).as_posix()):
        h.update(p.relative_to(folder).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


@contextmanager
def staging() -> Iterator[Path]:
    ENV.cache.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="stage-", dir=ENV.cache))
    try:
        yield d
    finally:
        with suppress(OSError):
            rmtree(d)


def unpack(archive: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(out)  # zipfile drops absolute paths and '..'
        return
    try:
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(out, filter="data")
            else:
                t.extractall(out)  # noqa: S202 (Python < 3.12 has no filter)
    except tarfile.TarError:
        die("the download isn't a zip or tar archive")


def fetch_archive(url: str, tmp: Path) -> tuple[Path, str]:
    """Download and unpack a mod archive; returns (the mod's root folder, the archive's sha256)."""
    archive = tmp / "download"
    download(url, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    out = tmp / "unpacked"
    unpack(archive, out)
    # Source archives wrap everything in one folder; release zips often don't.
    entries = [p for p in out.iterdir() if p.name not in ("__MACOSX", ".DS_Store")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0], digest
    return out, digest


def place(src: Path, dest: Path) -> None:
    """Copy a file into place by rename, so a running game keeps its old copy."""
    part = dest.with_name(f".{dest.name}.balala-part")
    shutil.copyfile(src, part)
    os.chmod(part, 0o755)
    try:
        os.replace(part, dest)
    except PermissionError:
        part.unlink()
        die(f"couldn't replace {dest.name}; close Balatro and try again")


def backup(path: Path) -> Path:
    ENV.backups.mkdir(parents=True, exist_ok=True)
    dest = ENV.backups / f"{path.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    move(path, dest)
    return dest


def has_local_work(target: Path, rec: dict | None) -> bool:
    """Whether a mod folder might hold someone's work. When in doubt, yes."""
    if rec is None:
        return True  # balala didn't install it
    if is_clone(target):
        return git_has_local_work(target, rec.get("base", rec.get("ref")) if rec.get("kind") == "git" else None)
    return rec.get("files") is None or fingerprint(target) != rec["files"]


def clear_target(target: Path, rec: dict | None) -> None:
    """Make room for a new install. Deletes balala's own untouched copies; moves
    anything that might hold someone's work to the backups folder instead."""
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        if has_local_work(target, rec):
            note(f"moved the old folder to {tilde(backup(target))}")
        else:
            rmtree(target)


# ── git ───────────────────────────────────────────────────────────────────


def _git(args: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess:
    if not shutil.which("git"):
        die(f"balala needs git: {ENV.plat.git_hint}")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # fail on missing/private repos instead of prompting
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    r = _git(args, cwd)
    if check and r.returncode != 0:
        die(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def git_ok(*args: str, cwd: Path | None = None) -> bool:
    return _git(args, cwd).returncode == 0


def is_clone(path: Path) -> bool:
    return (path / ".git").exists()


def git_dirty(path: Path, untracked: bool = False) -> bool:
    """Edits to tracked files (and, with `untracked`, new or ignored files too). True if git can't tell."""
    args = ["status", "--porcelain", "--ignored" if untracked else "--untracked-files=no"]
    r = _git(tuple(args), path)
    return r.returncode != 0 or bool(r.stdout.strip())


def git_has_local_work(path: Path, base: str | None) -> bool:
    """Any edits or files of the user's, or commits upstream doesn't have. A clone
    still at `base` (the commit balala last put there) has no local commits, even
    if upstream has rewritten its history since. Anything git can't answer counts
    as work."""
    if git_dirty(path, untracked=True):
        return True
    head = _git(("rev-parse", "HEAD"), path)
    if head.returncode != 0:
        return True
    if base and head.stdout.strip() == base:
        return False
    ahead = _git(("rev-list", "--count", "@{u}..HEAD"), path)
    return ahead.returncode != 0 or int(ahead.stdout.strip() or 0) > 0


def norm_remote(url: str) -> str:
    u = url.strip().lower()
    u = re.sub(r"^git@github\.com:", "https://github.com/", u)
    u = re.sub(r"^(https?://)(www\.)?", "https://", u)
    u = u.rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def same_remote(a: str | None, b: str | None) -> bool:
    return bool(a and b) and norm_remote(a) == norm_remote(b)


# ── sources ───────────────────────────────────────────────────────────────


@dataclass
class Source:
    url: str
    kind: str  # github | git | archive
    name: str  # default folder name in Mods/
    repo: str | None = None  # owner/name, for GitHub
    branch: str | None = None


def _archive_stem(filename: str) -> str:
    return re.sub(r"(\.zip|\.tar\.gz|\.tgz)$", "", filename, flags=re.IGNORECASE)


def parse_source(link: str) -> Source:
    s = link.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.IGNORECASE):  # a local path?
        p = Path(s).expanduser()
        if p.exists():
            if p.is_file() and p.name.lower().endswith(ARCHIVE_EXTS):
                return Source(p.resolve().as_uri(), "archive", _archive_stem(p.name))
            if is_clone(p):
                return Source(p.resolve().as_uri(), "git", p.resolve().name)
            die(f"{link} is a local folder but not a git repo (give a .zip, or a git checkout)")
    if re.fullmatch(r"[A-Za-z0-9-]+/[\w.-]+", s) and not s.lower().endswith(ARCHIVE_EXTS):
        s = "https://github.com/" + s  # owner/repo (GitHub owners can't contain dots)
    s = re.sub(r"^git@github\.com:", "https://github.com/", s)
    if re.match(r"^(www\.)?github\.com/", s, re.IGNORECASE):
        s = "https://" + s
    u = urllib.parse.urlparse(s)
    host = u.netloc.lower()
    segs = [p for p in u.path.split("/") if p]
    is_github = host in ("github.com", "www.github.com")

    if u.scheme in ("http", "https", "file") and u.path.lower().endswith(ARCHIVE_EXTS):
        name = segs[1] if is_github and len(segs) >= 2 else _archive_stem(segs[-1])
        return Source(s, "archive", name)
    if is_github:
        if len(segs) < 2:
            die(f"{link} isn't a GitHub repo link")
        owner, repo = segs[0], segs[1][:-4] if segs[1].endswith(".git") else segs[1]
        branch = "/".join(segs[3:]) if len(segs) > 3 and segs[2] == "tree" else None
        return Source(f"https://github.com/{owner}/{repo}", "github", repo, f"{owner}/{repo}", branch)
    if u.scheme in ("http", "https", "ssh", "git", "file") or re.match(r"^[\w.-]+@[\w.-]+:", s):
        tail = re.split(r"[/:]", s.rstrip("/"))[-1]
        return Source(s, "git", tail[:-4] if tail.endswith(".git") else tail)
    die(f"don't know how to install {link!r}: give a GitHub repo, a git URL or a .zip link")


def source_for(name: str) -> Source:
    rec = STATE["mods"].get(name)
    if rec is None:
        return parse_source(CORE[name])
    src = parse_source(rec["source"])
    src.branch = rec.get("branch") or src.branch
    return src


def display_source(url: str) -> str:
    if url.startswith("https://github.com/"):
        return url[len("https://github.com/") :]
    if url.startswith("file://"):
        return tilde(Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path)))
    return url


def release_archive_url(repo: str, rel: dict) -> str:
    """The release's own zip if it has exactly one obvious one, else the tag's source zip."""
    zips = [a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")]
    if len(zips) > 1:
        stem = repo.split("/")[1].lower()
        zips = [a for a in zips if stem in a["name"].lower()]
    if len(zips) == 1:
        return zips[0]["browser_download_url"]
    tag = urllib.parse.quote(rel["tag_name"], safe="")
    return f"https://codeload.github.com/{repo}/zip/refs/tags/{tag}"


def check_name(name: str) -> str:
    if (
        not name
        or name in (".", "..")
        or name.startswith(".")
        or name.lower() == "lovely"
        or re.search(r'[\\/:*?"<>|]', name)
    ):
        die(f"{name!r} can't be a mod folder name; choose one with --name")
    return name


# ── mods ──────────────────────────────────────────────────────────────────


def describe(rec: dict | None) -> str:
    """How an install reads: a release tag, branch@commit, or sha256:… for an archive."""
    if not rec:
        return ""
    if rec.get("kind") == "git":
        return f"{rec.get('on') or rec.get('branch') or 'HEAD'}@{rec['ref'][:7]}"
    if rec.get("kind") == "archive":
        return f"sha256:{rec['ref'][:7]}"
    return rec["ref"]


def transition(old: str, new: str) -> str:
    if not old or old == new:
        return new
    a, _, _ = old.partition("@")
    b, _, y = new.partition("@")
    return f"{old} → {y}" if y and a == b else f"{old} → {new}"


def outcome(old: dict | None, new: dict) -> Result:
    if old is None:
        return Result("installed", describe(new))
    return Result("updated", transition(describe(old), describe(new)))


def record(
    name: str, src: Source, channel: str, kind: str, ref: str, *, base: str | None = None, files: str | None = None
) -> dict:
    rec = {
        "source": src.url,
        "branch": src.branch,  # what was asked for; None means the default branch
        "channel": channel,  # what was asked for: latest | stable
        "kind": kind,  # what's there: git | release | archive
        "ref": ref,  # the commit checked out, or the tag / archive hash installed
        "updated": now(),
    }
    if kind == "git":
        rec["base"] = base  # the last commit balala itself put there; None for a clone it adopted with work in it
        rec["on"] = git("rev-parse", "--abbrev-ref", "HEAD", cwd=ENV.mods / name, check=False)
    else:
        rec["files"] = files  # fingerprint of what balala unpacked, to spot later edits
    STATE["mods"][name] = rec
    save_state()
    return rec


def canonical_name(name: str) -> str:
    """Folder names are case-insensitive on macOS and Windows, so treat them that way everywhere."""
    return next((k for k in STATE["mods"] if k.lower() == name.lower()), name)


def ensure_mod(src: Source, channel: str, name: str | None = None) -> Result:
    """Install a mod, update it, or move it to another channel. Safe to repeat."""
    name = canonical_name(check_name(name or src.name))
    ENV.mods.mkdir(parents=True, exist_ok=True)
    target = ENV.mods / name
    rec = STATE["mods"].get(name)
    if rec and not same_remote(rec["source"], src.url):
        note(f"replaces {display_source(rec['source'])}")
    if src.kind == "archive":
        return _install_archive(name, target, src, rec)
    if channel == "stable":
        if src.kind != "github":
            note("only GitHub repos have releases; tracking the newest commit")
            channel = "latest"
        elif src.branch:
            note(f"tracking branch {src.branch}, so releases don't apply")
            channel = "latest"
        else:
            rel = newest_release(src.repo)
            if rel:
                return _install_release(name, target, src, rel, rec)
            if not (rec and rec.get("channel") == "stable" and rec.get("kind") == "git"):
                note("no releases yet; tracking the newest commit until there is one")
    return _install_git(name, target, src, rec, channel)


def _install_archive(name: str, target: Path, src: Source, rec: dict | None) -> Result:
    with staging() as tmp:
        root, digest = fetch_archive(src.url, tmp)
        if rec and rec.get("kind") == "archive" and rec.get("ref") == digest and target.is_dir():
            if rec["source"] != src.url:  # same files from a new link: update from the new one from now on
                STATE["mods"][name] = {**rec, "source": src.url, "updated": now()}
                save_state()
            return Result("current", describe(rec))
        files = fingerprint(root)
        clear_target(target, rec)
        move(root, target)
    return outcome(rec, record(name, src, "latest", "archive", digest, files=files))


def _install_release(name: str, target: Path, src: Source, rel: dict, rec: dict | None) -> Result:
    tag = rel["tag_name"]
    if (
        rec
        and rec.get("kind") == "release"
        and rec.get("ref") == tag
        and target.is_dir()
        and same_remote(rec["source"], src.url)
    ):
        return Result("current", tag)
    with staging() as tmp:
        root, _ = fetch_archive(release_archive_url(src.repo, rel), tmp)
        files = fingerprint(root)
        clear_target(target, rec)
        move(root, target)
    return outcome(rec, record(name, src, "stable", "release", tag, files=files))


def _install_git(name: str, target: Path, src: Source, rec: dict | None, channel: str) -> Result:
    # Reuse the clone if it's this repo on the branch asked for. (A clone of a /tree/ branch
    # doesn't count when the default branch is asked for: that's a switch.)
    same_ask = rec is None or rec.get("kind") != "git" or rec.get("branch") == src.branch
    if (
        same_ask
        and is_clone(target)
        and same_remote(git("remote", "get-url", "origin", cwd=target, check=False), src.url)
    ):
        on = git("rev-parse", "--abbrev-ref", "HEAD", cwd=target, check=False)
        if not src.branch or on == src.branch:
            if rec is None:
                note("adopted the clone that was already there")
            base = rec.get("base", rec["ref"]) if rec and rec.get("kind") == "git" else None
            status, old, head = _git_sync(target, base)
            # After a sync, HEAD is upstream's, so it's a clean base. A kept clone keeps the old one.
            new = record(name, src, channel, "git", head, base=base if status == "kept" else head)
            if status == "current":
                return Result("current", describe(new))
            if status == "kept":
                return Result("kept", f"{describe(new)}, left alone")
            return Result("updated", transition(describe(rec) if rec else f"{on}@{old[:7]}", describe(new)))
    with staging() as tmp:
        dest = tmp / name
        args = ["clone", "--quiet", "--depth", "1", *(["--branch", src.branch] if src.branch else [])]
        try:
            git(*args, src.url, str(dest))
        except Fail:
            if src.repo and api(f"repos/{src.repo}") is None:
                die("no such GitHub repo (or it's private)")
            raise
        clear_target(target, rec)
        move(dest, target)
    head = git("rev-parse", "HEAD", cwd=target)
    return outcome(rec, record(name, src, channel, "git", head, base=head))


def _git_sync(path: Path, base: str | None) -> tuple[str, str, str]:
    """Fast-forward a clone to upstream; returns (status, old HEAD, new HEAD). If upstream
    rewrote its history, reset only a clone that's exactly as balala left it, with
    nothing of the user's in it: local work is never discarded."""
    git("fetch", "--quiet", "origin", cwd=path)
    head = git("rev-parse", "HEAD", cwd=path)
    up = git("rev-parse", "@{u}", cwd=path)
    if head == up:
        return "current", head, head
    if not git_dirty(path) and git_ok("merge-base", "--is-ancestor", head, up, cwd=path):
        git("merge", "--ff-only", "--quiet", up, cwd=path)  # refuses by itself if it would clobber untracked files
    elif head == base and not git_dirty(path, untracked=True):
        git("reset", "--hard", "--quiet", up, cwd=path)
        note("upstream rewrote its history; reset to match")
    else:
        note("has local changes or commits. Commit or discard them, or remove and reinstall it")
        return "kept", head, head
    return "updated", head, up


# ── mod metadata (for dependency warnings) ────────────────────────────────


def mod_headers(folder: Path) -> list[dict]:
    """The Steamodded JSON headers at the top of a mod folder."""
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("id"), str) and "main_file" in d:
            out.append(d)
    return out


def _dep_id(spec) -> str:
    return str(spec).split("(")[0].strip().lower()


def mod_folders() -> list[Path]:
    if not ENV.mods.is_dir():
        return []
    return sorted(
        (d for d in ENV.mods.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name != "lovely"),
        key=lambda d: d.name.lower(),
    )


def installed_ids() -> set[str]:
    ids = {"balatro"}
    if STATE.get("lovely"):
        ids.add("lovely")
    for d in mod_folders():
        if (d / "version.lua").is_file() and (d / "src").is_dir() and (d / "lovely").is_dir():
            ids.add("steamodded")  # Steamodded has no JSON header of its own
        for h in mod_headers(d):
            ids.add(h["id"].lower())
            ids.update(_dep_id(p) for p in h.get("provides") or [])
        for lua in d.glob("*.lua"):  # old-style "--- MOD_ID:" headers
            try:
                m = re.search(r"^--- MOD_ID:\s*(\S+)", lua.read_text(errors="replace")[:2000], re.MULTILINE)
            except OSError:
                continue
            if m:
                ids.add(m.group(1).lower())
    return ids


def missing_deps(name: str) -> list[str]:
    have = installed_ids()
    out = []
    for h in mod_headers(ENV.mods / name):
        for dep in h.get("dependencies") or []:
            if not any(_dep_id(alt) in have for alt in str(dep).split("|")):
                out.append(f"{h['id']} needs {dep}, which isn't installed")
    return out


# ── lovely ────────────────────────────────────────────────────────────────


def lovely_files() -> list[str]:
    rec = STATE.get("lovely") or {}
    return rec.get("files") or ENV.plat.lovely_default_files()


def lovely_present() -> bool:
    return all((ENV.game.dir / f).is_file() for f in lovely_files())


def ensure_lovely() -> Result:
    """lovely is compiled Rust, so it's always the newest release, whatever the channel."""
    game = ENV.game
    rel = newest_release(LOVELY_REPO) or die("lovely has no releases")
    tag = rel["tag_name"]
    rec = STATE.get("lovely")
    if rec and rec.get("ref") == tag and lovely_present():
        result = Result("current", tag)
    else:
        asset_name = ENV.plat.lovely_asset()
        asset = next((a for a in rel["assets"] if a["name"] == asset_name), None)
        if asset is None:
            die(f"lovely {tag} has no {asset_name}")
        with staging() as tmp:
            archive = tmp / asset_name
            download(asset["browser_download_url"], archive)
            unpack(archive, tmp / "unpacked")
            files = ENV.plat.lovely_files(tmp / "unpacked")
            for f in files:
                place(f, game.dir / f.name)
        names = [f.name for f in files]
        stale = set(rec.get("files") or []) if rec else set()
        if ENV.plat.marker == "Balatro.exe":
            stale |= set(LOVELY_DLLS)  # e.g. version.dll from lovely < 0.10, which would load twice
        for n in stale - set(names):
            try:
                (game.dir / n).unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                die(f"couldn't remove the old {n}; close Balatro and try again")
        STATE["lovely"] = {"ref": tag, "files": names, "updated": now()}
        save_state()
        if rec is None:
            result = Result("installed", tag)
        elif rec.get("ref") == tag:
            result = Result("updated", f"{tag} (reinstalled)")
        else:
            result = Result("updated", f"{rec.get('ref')} → {tag}")
    for hint in ENV.plat.hints(game, lovely_files()):
        note(hint)
    return result


# ── commands ──────────────────────────────────────────────────────────────


def _mod_job(name: str, channel: str | None) -> Callable[[], Result]:
    # With no channel given, a mod keeps the one it was installed with (new ones get latest).
    def job() -> Result:
        rec = STATE["mods"].get(name)
        return ensure_mod(source_for(name), channel or (rec["channel"] if rec else "latest"), name)

    return job


def cmd_setup(channel: str | None) -> int:
    failed = run_jobs([("lovely", ensure_lovely), *((n, _mod_job(n, channel)) for n in CORE)])
    if not failed:
        STATE["setup"] = now()  # from now on, a core mod you remove stays removed
        save_state()
    return failed


def cmd_install(links: list[str], channel: str, name: str | None) -> int:
    if name and len(links) > 1:
        die("--name only works with one link")
    sources = [parse_source(link) for link in links]  # bad links fail before anything changes
    failed = run_jobs([(name or s.name, lambda s=s: ensure_mod(s, channel, name)) for s in sources])
    for s in sources:
        if (name or s.name) in STATE["mods"]:
            for w in missing_deps(name or s.name):
                warn(w)
    return failed


def cmd_update(channel: str | None) -> int:
    if not STATE.get("setup"):  # never set up: setting up is the update
        failed = cmd_setup(channel)
        rest = [n for n in STATE["mods"] if n not in CORE]
        return failed + (run_jobs([(n, _mod_job(n, channel)) for n in rest]) if rest else 0)
    names = [*(n for n in CORE if n in STATE["mods"]), *(n for n in STATE["mods"] if n not in CORE)]
    return run_jobs([("lovely", ensure_lovely), *((n, _mod_job(n, channel)) for n in names)])


def cmd_list() -> int:
    rows = [("lovely", "release", describe(STATE.get("lovely")) or "not installed", LOVELY_REPO)]
    for n in [*CORE, *sorted((k for k in STATE["mods"] if k not in CORE), key=str.lower)]:
        rec = STATE["mods"].get(n)
        if rec is None:
            rows.append((n, "", "not installed", CORE[n]))
            continue
        version = describe(rec) + ("" if (ENV.mods / n).is_dir() else " (folder missing)")
        rows.append((n, rec["channel"] if rec["kind"] != "archive" else "file", version, display_source(rec["source"])))
    managed = set(STATE["mods"])
    rows += [(d.name, "", "not managed by balala", "") for d in mod_folders() if d.name not in managed]
    header = ("NAME", "CHANNEL", "VERSION", "SOURCE")
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(3)]

    def fmt(r):
        return f"{r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  {r[2]:<{widths[2]}}  {r[3]}".rstrip()

    print(paint(fmt(header), "2"))
    for r in rows:
        print(fmt(r))
    print(paint(f"\nMods folder: {tilde(ENV.mods)}", "2"))
    return 0


def cmd_remove(queries: list[str]) -> int:
    known = list(STATE["mods"]) + [d.name for d in mod_folders() if d.name not in STATE["mods"]]
    hits = []
    for q in queries:
        hit = next((k for k in known if k == q), None) or next((k for k in known if k.lower() == q.lower()), None)
        hits.append(hit or die(f"{q} isn't installed (see `balala list`)"))

    def remove(name: str) -> Result:
        target = ENV.mods / name
        if target.exists() or target.is_symlink():
            clear_target(target, STATE["mods"].get(name))
        STATE["mods"].pop(name, None)
        save_state()
        if name in CORE:
            note("part of setup; `balala setup` brings it back")
        return Result("removed")

    return run_jobs([(h, lambda h=h: remove(h)) for h in dict.fromkeys(hits)])


def newest_log() -> Path | None:
    d = ENV.mods / "lovely" / "log"
    logs = list(d.glob("*.log")) if d.is_dir() else []
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def cmd_logs(follow: bool, lines: int) -> int:
    log = newest_log()
    if log is None and not follow:
        die("no lovely logs yet; launch the game first")
    if log:
        print(paint(f"── {tilde(log)}", "2"))
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:] if lines > 0 else []
        for line in tail:
            print(line)
    if not follow:
        return 0
    print(paint("── following; Ctrl-C to stop", "2"), flush=True)
    try:
        _follow(log)
    except KeyboardInterrupt:
        print()
    return 0


def _follow(log: Path | None) -> None:
    """Print a log as it grows, moving on to the next one when the game is relaunched."""
    skip_existing = True
    while True:
        while log is None:
            time.sleep(0.5)
            log, skip_existing = newest_log(), False
        with open(log, encoding="utf-8", errors="replace") as f:
            if skip_existing:
                f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    continue
                sys.stdout.flush()
                time.sleep(0.5)
                latest = newest_log()
                if latest and latest != log:
                    break
        log, skip_existing = latest, False
        print(paint(f"── {tilde(log)}", "2"))


def cmd_launch() -> int:
    game = ENV.game
    if not STATE.get("setup"):
        print("First run: setting up lovely, Steamodded and Amulet.")
        if cmd_setup(None):
            die("setup didn't finish, so Balatro wasn't launched")
    elif not lovely_present() and run_jobs([("lovely", ensure_lovely)]):
        # e.g. a Steam reinstall wiped it, and the game can't load mods without it
        die("lovely is missing and couldn't be reinstalled")
    if game_running():
        print("Balatro is already running.")
        return 0
    for hint in ENV.plat.hints(game, lovely_files()):
        warn(hint)
    ENV.plat.launch(game)
    mods = len(mod_folders())
    tag = (STATE.get("lovely") or {}).get("ref", "")
    print(
        f"{paint('▶', '1;32')} Balatro is starting with lovely {tag} and {plural(mods, 'mod')}. "
        f"{paint('Logs: balala logs -f', '2')}"
    )
    return 0


# ── cli ───────────────────────────────────────────────────────────────────

EPILOG = """\
With no command, balala launches Balatro with your mods, setting everything up
the first time.

channels (setup, install, update):
  -l, --latest   the newest commit on the default branch (the default)
  -s, --stable   the newest release
  update keeps each mod on the channel it was installed with unless you pass one.
  lovely is compiled, so it always uses its newest release.

examples:
  balala                                  launch
  balala install SpectralPack/Cryptid     install a mod from GitHub
  balala install <url> --stable           ...at its newest release
  balala update                           update everything
  balala logs -f                          watch the game's log
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="balala",
        description="One command for modded Balatro.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-V", "--version", action="version", version=f"balala {__version__}")
    sub = p.add_subparsers(dest="cmd", title="commands", metavar="<command>")

    def channels(sp, default_help):
        g = sp.add_mutually_exclusive_group()
        g.add_argument(
            "-l",
            "--latest",
            dest="channel",
            action="store_const",
            const="latest",
            help="track the newest commit" + (" (default)" if default_help else ""),
        )
        g.add_argument(
            "-s", "--stable", dest="channel", action="store_const", const="stable", help="use the newest release"
        )

    sub.add_parser(
        "launch",
        help="launch Balatro (the same as no command)",
        description="Launch Balatro with lovely, setting things up the first time.",
    )
    sp = sub.add_parser(
        "setup",
        help="install lovely, Steamodded and Amulet",
        description="Install lovely, Steamodded and Amulet, or move them to another channel.",
    )
    channels(sp, True)
    sp = sub.add_parser(
        "install",
        help="install mods from GitHub, git or .zip links",
        description="Install mods. A link can be a GitHub repo (owner/repo, a URL, or a "
        ".../tree/<branch> URL), any git URL, a .zip/.tar.gz URL, or a local "
        "archive or git checkout. Installing an installed mod updates it.",
    )
    sp.add_argument("links", nargs="+", metavar="LINK")
    sp.add_argument("-n", "--name", help="folder name in Mods/ (default: the repo's name)")
    channels(sp, True)
    sp = sub.add_parser(
        "update",
        help="update lovely, Steamodded, Amulet and your mods",
        description="Update everything balala installed. Each mod stays on its channel "
        "unless you pass -l or -s, which moves them all.",
    )
    channels(sp, False)
    sub.add_parser("list", help="show what's installed", description="Show what's installed.")
    sp = sub.add_parser("remove", help="remove mods", description="Remove mods.")
    sp.add_argument("names", nargs="+", metavar="NAME")
    sp = sub.add_parser("logs", help="show the game's latest log", description="Show lovely's latest log.")
    sp.add_argument("-f", "--follow", action="store_true", help="keep printing as the game writes")
    sp.add_argument("-n", "--lines", type=int, default=40, metavar="N", help="lines to show (default 40)")
    return p


def main(argv: list[str] | None = None) -> int:
    global ENV, STATE, _COLOR
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # never crash on a glyph a console can't show
    _COLOR = not os.environ.get("NO_COLOR") and _enable_vt()
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        ENV = Env()
        STATE = load_state()
        cmd = args.cmd or "launch"
        if cmd == "launch":
            return cmd_launch()
        if cmd == "list":
            return cmd_list()
        if cmd == "logs":
            return cmd_logs(args.follow, args.lines)
        if cmd == "setup":
            failed = cmd_setup(args.channel or "latest")
        elif cmd == "install":
            failed = cmd_install(args.links, args.channel or "latest", args.name)
        elif cmd == "update":
            failed = cmd_update(args.channel)
        else:
            failed = cmd_remove(args.names)
        if game_running():
            print(paint("Balatro is running; changes apply the next time it starts.", "2"))
        return 1 if failed else 0
    except Fail as e:
        error(str(e))
        return 1
    except OSError as e:
        error(os_error(e))
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
