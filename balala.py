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

__version__ = "0.1.1"

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


def run_jobs(jobs: list[tuple[str, Callable[[], Result]]], width: int = 0) -> int:
    """Run jobs one line each, carrying on past failures; returns how many failed."""
    global _notes
    width = max(width, *(len(label) for label, _ in jobs), 0)
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


def delete(target: Path) -> None:
    """Remove a mod folder (or just the link, for a symlinked dev checkout)."""
    if target.is_symlink():
        target.unlink()
    elif target.exists():
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


def git_dirty(path: Path) -> bool:
    """Uncommitted edits to tracked files."""
    return bool(git("status", "--porcelain", "--untracked-files=no", cwd=path, check=False))


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
    """How an install reads: a release tag, branch@commit, sha256:… for an archive, or a hand install's version."""
    if not rec:
        return ""
    kind = rec.get("kind")
    if kind == "git":
        return f"{rec.get('on') or rec.get('branch') or 'HEAD'}@{rec['ref'][:7]}"
    if kind == "archive":
        return f"sha256:{rec['ref'][:7]}"
    if kind == "manual":
        return f"{rec['ref'] or '?'} (by hand)"
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


def record(name: str, src: Source, channel: str, kind: str, ref: str) -> dict:
    rec = {
        "source": src.url,
        "branch": src.branch,  # what was asked for; None means the default branch
        "channel": channel,  # what was asked for: latest | stable
        "kind": kind,  # what's there: git | release | archive | manual (installed by hand, not yet replaced)
        "ref": ref,  # the commit checked out, the tag or archive hash installed, or a hand install's version
        "updated": now(),
    }
    if kind == "git":
        rec["on"] = git("rev-parse", "--abbrev-ref", "HEAD", cwd=ENV.mods / name, check=False)
    STATE["mods"][name] = rec
    save_state()
    return rec


def canonical_name(name: str) -> str:
    """Folder names are case-insensitive on macOS and Windows, so treat them that way everywhere."""
    return next((k for k in STATE["mods"] if k.lower() == name.lower()), name)


def key_for(src: Source) -> str | None:
    """The name balala already manages this mod under, if any."""
    return next((k for k, r in STATE["mods"].items() if same_remote(r["source"], src.url)), None)


def core_name(name: str) -> str:
    """Where a core mod lives: its usual folder, or wherever a hand install of it was found."""
    return key_for(parse_source(CORE[name])) or name


def is_core(name: str) -> bool:
    rec = STATE["mods"].get(name)
    return name in CORE if rec is None else any(same_remote(rec["source"], parse_source(r).url) for r in CORE.values())


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
            if rec["source"] != src.url:  # the same files from a new link: update from the new one from now on
                STATE["mods"][name] = {**rec, "source": src.url, "updated": now()}
                save_state()
            return Result("current", describe(rec))
        delete(target)
        move(root, target)
    return outcome(rec, record(name, src, "latest", "archive", digest))


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
        delete(target)
        move(root, target)
    return outcome(rec, record(name, src, "stable", "release", tag))


def _install_git(name: str, target: Path, src: Source, rec: dict | None, channel: str) -> Result:
    # Reuse the clone if it's this repo on the branch asked for. (Asking for the default
    # branch after a /tree/<branch> link is a switch, so that reclones.)
    same_ask = rec is None or rec.get("kind") != "git" or rec.get("branch") == src.branch
    if (
        same_ask
        and is_clone(target)
        and same_remote(git("remote", "get-url", "origin", cwd=target, check=False), src.url)
    ):
        on = git("rev-parse", "--abbrev-ref", "HEAD", cwd=target, check=False)
        if not src.branch or on == src.branch:
            status, old, head = _git_sync(target)
            new = record(name, src, channel, "git", head)
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
        delete(target)
        move(dest, target)
    return outcome(rec, record(name, src, channel, "git", git("rev-parse", "HEAD", cwd=target)))


def _git_sync(path: Path) -> tuple[str, str, str]:
    """Bring a clone up to date with upstream; returns (status, old HEAD, new HEAD).
    Only uncommitted edits stop it: those are left alone."""
    git("fetch", "--quiet", "origin", cwd=path)
    head = git("rev-parse", "HEAD", cwd=path)
    up = git("rev-parse", "@{u}", cwd=path)
    if head == up:
        return "current", head, head
    if git_dirty(path):
        note("has uncommitted changes, so it was left alone")
        return "kept", head, head
    if git_ok("merge-base", "--is-ancestor", head, up, cwd=path):
        git("merge", "--ff-only", "--quiet", up, cwd=path)
    else:
        git("reset", "--hard", "--quiet", up, cwd=path)
        note("upstream's history changed, so it was reset to match (older commits are in `git reflog`)")
    return "updated", head, up


# ── mod metadata ──────────────────────────────────────────────────────────


def mod_headers(folder: Path) -> list[dict]:
    """The Steamodded JSON headers at the top of a mod folder."""
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"), strict=False)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("id"), str) and "main_file" in d:
            out.append(d)
    return out


def is_steamodded(folder: Path) -> bool:
    """Steamodded has no JSON header of its own, so recognize it by its layout."""
    return (folder / "version.lua").is_file() and (folder / "src").is_dir() and (folder / "lovely").is_dir()


def mod_ids(folder: Path) -> dict[str, str]:
    """The mod ids a folder declares, lowercased -> as written."""
    ids = {h["id"].lower(): h["id"] for h in mod_headers(folder)}
    if is_steamodded(folder):
        ids["steamodded"] = "Steamodded"
    for lua in folder.glob("*.lua"):  # old-style "--- MOD_ID:" headers
        try:
            m = re.search(r"^--- MOD_ID:\s*(\S+)", lua.read_text(errors="replace")[:2000], re.MULTILINE)
        except OSError:
            continue
        if m:
            ids.setdefault(m.group(1).lower(), m.group(1))
    return ids


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
        ids.update(mod_ids(d))
        for h in mod_headers(d):
            ids.update(_dep_id(p) for p in h.get("provides") or [])
    return ids


def missing_deps(name: str) -> list[str]:
    have = installed_ids()
    out = []
    for h in mod_headers(ENV.mods / name):
        for dep in h.get("dependencies") or []:
            if not any(_dep_id(alt) in have for alt in str(dep).split("|")):
                out.append(f"{h['id']} needs {dep}, which isn't installed")
    return out


def conflicts() -> list[str]:
    """Mods Steamodded will refuse to load together: one id in two folders, or a mod
    alongside one that replaces it (Amulet provides Talisman)."""
    owners: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    folders = mod_folders()
    for d in folders:
        for low, written in mod_ids(d).items():
            owners.setdefault(low, []).append(d.name)
            shown.setdefault(low, written)
    out = [f"{shown[i]} is installed twice ({' and '.join(f)}); remove one" for i, f in owners.items() if len(f) > 1]
    for d in folders:
        for h in mod_headers(d):
            for p in h.get("provides") or []:
                others = [f for f in owners.get(_dep_id(p), []) if f != d.name]
                if others:
                    out.append(
                        f"{h['id']} replaces {' and '.join(others)}; remove {'it' if len(others) == 1 else 'them'}"
                    )
    return out


# ── mods installed by hand, and finding mods by name ─────────────────────

CATALOG_URL = "https://thunderstore.io/c/balatro/api/v1/package/"
_catalog: list[dict] | None = None
_repo_ids: dict[str, set[str]] = {}
_dupes: dict[str, str] = {}  # folder -> the managed mod it's a second copy of
GITHUB_REPO_URL = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", re.IGNORECASE)


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _folder_stem(name: str) -> str:
    """'Cryptid-main' and 'Cryptid-0.5.17d' (GitHub zip folders) are Cryptid."""
    return re.sub(r"[-_ ](main|master|v?\d[\w.~+-]*)$", "", name, flags=re.IGNORECASE)


def _github_url(url) -> str | None:
    m = GITHUB_REPO_URL.match(str(url or ""))
    if not m:
        return None
    repo = m.group(2)[:-4] if m.group(2).endswith(".git") else m.group(2)
    return f"https://github.com/{m.group(1)}/{repo}"


def catalog() -> list[dict]:
    """Thunderstore's Balatro packages that link a GitHub repo, as {name, repo}. Cached for a
    day; an old copy, or nothing, if it can't be fetched."""
    global _catalog
    if _catalog is None:
        path = ENV.cache / "thunderstore.json"
        try:
            if not path.is_file() or time.time() - path.stat().st_mtime > 86400:
                ENV.cache.mkdir(parents=True, exist_ok=True)
                part = path.with_name(path.name + ".part")
                download(CATALOG_URL, part)
                os.replace(part, path)
        except (Fail, OSError):
            pass
        try:
            packages = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            packages = []
        _catalog = []
        for p in packages if isinstance(packages, list) else []:
            if isinstance(p, dict) and not p.get("is_deprecated"):
                repo = _github_url(((p.get("versions") or [{}])[0] or {}).get("website_url"))
                if repo:
                    _catalog.append({"name": str(p.get("name", "")), "repo": repo})
    return _catalog


def catalog_matches(names: set[str]) -> list[str]:
    """Repos of catalog packages whose name, or whose repo's name, is one of `names` (normalized)."""
    hits: dict[str, str] = {}
    for p in catalog():
        if names & {_norm(p["name"]), _norm(p["repo"].rsplit("/", 1)[-1])}:
            hits.setdefault(norm_remote(p["repo"]), p["repo"])
    return list(hits.values())


def manifest_urls(folder: Path) -> list[str]:
    """GitHub repos a mod's own metadata points at, like a Thunderstore manifest's website_url."""
    urls = []
    for p in sorted(folder.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"), strict=False)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            for key in ("website_url", "website", "homepage", "repository", "repo", "source"):
                url = _github_url(d.get(key))
                if url:
                    urls.append(url)
    return list(dict.fromkeys(urls))


def repo_ids(repo: str) -> set[str]:
    """The (lowercased) mod ids in a GitHub repo's top-level Steamodded headers."""
    if repo not in _repo_ids:
        ids = set()
        listing = api(f"repos/{repo}/contents")
        for f in listing if isinstance(listing, list) else []:
            if f.get("type") != "file" or not f["name"].lower().endswith(".json") or not f.get("download_url"):
                continue
            try:
                with _open(f["download_url"]) as r:
                    d = json.loads(r.read().decode("utf-8-sig", errors="replace"), strict=False)
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and isinstance(d.get("id"), str) and "main_file" in d:
                ids.add(d["id"].lower())
        _repo_ids[repo] = ids
    return _repo_ids[repo]


def search_github(name: str) -> list[str]:
    """Repos named `name`, most-starred first, forks excluded."""
    q = urllib.parse.quote(f"{name} in:name fork:false")
    found = api(f"search/repositories?q={q}&sort=stars&order=desc&per_page=10") or {}
    return [it["full_name"] for it in found.get("items", []) if _norm(it["name"]) == _norm(name)]


def _verified(url: str, ids: set[str]) -> Source | None:
    """`url` as a source, if that repo declares one of these mod ids."""
    src = parse_source(url)
    return src if src.repo and {_norm(i) for i in ids} & {_norm(i) for i in repo_ids(src.repo)} else None


def identify(folder: Path) -> Source | None:
    """Work out where a hand-installed mod came from: its git remote; else its own metadata,
    Thunderstore, or a GitHub search. Those last three count only if the repo declares the same
    mod id, so a different mod with the same name is never mistaken for it."""
    if folder.is_symlink():
        return None  # a dev checkout linked in: leave it be
    if is_clone(folder):
        url = git("remote", "get-url", "origin", cwd=folder, check=False)
        try:
            return parse_source(url) if url else None
        except Fail:
            return None
    if is_steamodded(folder):
        return parse_source(CORE["smods"])
    headers = mod_headers(folder)
    ids = set(mod_ids(folder))
    if not ids:
        return None  # no Steamodded header: nothing to check a match against
    names = {_norm(v) for h in headers for v in (h.get("id"), h.get("name")) if isinstance(v, str)}
    candidates = manifest_urls(folder) + catalog_matches((names | {_norm(_folder_stem(folder.name))}) - {""})
    for url in dict.fromkeys(candidates):
        src = _verified(url, ids)
        if src:
            return src
    for h in headers:
        for full in search_github(h["id"]):
            src = _verified(f"https://github.com/{full}", ids)
            if src:
                return src
    return None


def find_by_name(name: str) -> Source | None:
    """A mod by name, e.g. "Cryptid": a Thunderstore package or GitHub repo of that name whose
    Steamodded header has that id."""
    wanted = {_norm(name)}
    candidates = catalog_matches(wanted) + [f"https://github.com/{full}" for full in search_github(name)]
    for url in dict.fromkeys(candidates):
        src = _verified(url, wanted)
        if src:
            return src
    return None


def guess_channel(folder: Path, src: Source) -> str:
    """A hand install looks like a release if its folder name or header version names one of the repo's tags."""
    if src.kind != "github":
        return "latest"
    hints = {_norm(h.get("version", "")).lstrip("v") for h in mod_headers(folder)}
    m = re.search(r"[-_ ](v?\d[\w.~+-]*)$", folder.name)
    if m:
        hints.add(_norm(m.group(1)).lstrip("v"))
    hints.discard("")
    if not hints:
        return "latest"
    tags = {_norm(r.get("tag_name", "")).lstrip("v") for r in api(f"repos/{src.repo}/releases?per_page=100") or []}
    return "stable" if hints & tags else "latest"


def adopt(folder: Path, src: Source) -> dict:
    if is_clone(folder):
        return record(folder.name, src, "latest", "git", git("rev-parse", "HEAD", cwd=folder))
    version = next((h["version"] for h in mod_headers(folder) if isinstance(h.get("version"), str)), "")
    return record(folder.name, src, guess_channel(folder, src), "manual", version)


def scan(tidy: bool) -> None:
    """Take over mods installed by hand, so they're listed and updated like the rest.
    With `tidy`, also delete second copies of a mod balala already manages (Steamodded
    can't load two)."""
    known = {k.lower() for k in STATE["mods"]}
    folders = sorted(
        (d for d in mod_folders() if d.name.lower() not in known), key=lambda d: (not is_clone(d), d.name.lower())
    )
    misses = STATE.setdefault("unknown", {})  # folder -> when balala last failed to place it
    for gone in set(misses) - {d.name for d in folders}:
        del misses[gone]
    managed = {norm_remote(r["source"]): k for k, r in STATE["mods"].items()}
    groups: dict[str, list[tuple[Path, Source]]] = {}
    for d in folders:
        if time.time() - misses.get(d.name, 0) < 86400:
            continue
        try:
            src = identify(d)
        except Fail:
            continue  # offline, say: try again next time
        if src is None:
            misses[d.name] = time.time()
            continue
        misses.pop(d.name, None)
        groups.setdefault(norm_remote(src.url), []).append((d, src))
    lines = []
    for key, found in groups.items():
        if key not in managed:  # keep the clone, else the folder named like the repo, else the shortest name
            d, src = min(
                found,
                key=lambda f: (
                    not is_clone(f[0]),
                    _folder_stem(f[0].name).lower() != f[1].name.lower(),
                    len(f[0].name),
                    f[0].name.lower(),
                ),
            )
            found.remove((d, src))
            managed[key] = d.name
            rec = adopt(d, src)
            lines.append((paint("+", "1;32"), d.name, f"found {describe(rec)} from {display_source(src.url)}"))
        for d, _ in found:
            _dupes[d.name] = managed[key]
            if tidy:
                delete(d)
                lines.append((paint("−", "1;31"), d.name, f"deleted: a second copy of {managed[key]}"))
    save_state()
    width = max((len(n) for _, n, _ in lines), default=0)
    for glyph, n, text in lines:
        print(f"  {glyph} {n:<{width}}  {text}")


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


# ── balala itself ─────────────────────────────────────────────────────────

SELF_REPO = "a9lim/balala"


def _version(s: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", s)[:3])


def upgrade_command(prefix: Path, installer: str, spec: str, which=shutil.which) -> list[str]:
    """The command that installs `spec` over this copy of balala, by the tool that installed it."""
    if (prefix / "uv-receipt.toml").is_file() and which("uv"):  # a `uv tool` environment
        return ["uv", "tool", "install", "--force", spec]
    if (prefix / "pipx_metadata.json").is_file() and which("pipx"):
        return ["pipx", "install", "--force", spec]
    if installer == "uv" and which("uv"):
        return ["uv", "pip", "install", "--reinstall", "--no-deps", "--python", sys.executable, spec]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", spec]


def how_installed() -> tuple[str, dict | None]:
    """("checkout" | "package" | "file", the package's metadata when it's a package)."""
    here = Path(__file__).resolve()
    if (here.parent / ".git").exists():
        return "checkout", None
    try:
        from importlib import metadata

        dist = metadata.distribution("balala")
        if any(Path(str(dist.locate_file(f))).resolve() == here for f in dist.files or [] if f.name == "balala.py"):
            direct = json.loads(dist.read_text("direct_url.json") or "null")
            return "package", {"installer": (dist.read_text("INSTALLER") or "").strip(), "direct": direct}
    except Exception:
        pass
    return "file", None


def ensure_self() -> Result:
    """Update balala to its newest release, the way it was installed. (Releases only: it's the tool
    doing the updating, so it shouldn't track a commit that might be broken.)"""
    kind, meta = how_installed()
    if kind == "checkout":
        return Result("current", f"{__version__} (a git checkout: update it with git)")
    rel = newest_release(SELF_REPO)
    if not rel or _version(rel["tag_name"]) <= _version(__version__):
        return Result("current", __version__)
    new = rel["tag_name"].lstrip("v")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    if kind == "file":
        url = assets.get("balala.py") or die(f"release {rel['tag_name']} has no balala.py")
        here = Path(__file__).resolve()
        part = here.with_name(f".{here.name}.balala-part")
        download(url, part)
        os.chmod(part, here.stat().st_mode)
        os.replace(part, here)
    else:
        wheel = next((u for n, u in assets.items() if n.endswith(".whl")), None)
        spec = f"balala=={new}" if not meta["direct"] or not wheel else f"balala @ {wheel}"  # PyPI, or the release
        cmd = upgrade_command(Path(sys.prefix), meta["installer"], spec)
        if os.name == "nt":  # Windows won't let the running balala.exe be replaced
            note(f"to finish, run: {subprocess.list2cmdline(cmd)}")
            return Result("kept", f"{__version__} → {new} available")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
            die(f"{subprocess.list2cmdline(cmd)} failed: {tail[0]}")
    note("takes effect the next time you run it")
    return Result("updated", f"{__version__} → {new}")


# ── commands ──────────────────────────────────────────────────────────────


def _mod_job(name: str, channel: str | None) -> Callable[[], Result]:
    # With no channel given, a mod keeps the one it was installed with (new ones get latest).
    def job() -> Result:
        rec = STATE["mods"].get(name)
        return ensure_mod(source_for(name), channel or (rec["channel"] if rec else "latest"), name)

    return job


def cmd_setup(channel: str | None, width: int = 0) -> int:
    jobs = [("lovely", ensure_lovely), *((n, _mod_job(n, channel)) for n in map(core_name, CORE))]
    failed = run_jobs(jobs, width)
    if not failed:
        STATE["setup"] = now()  # from now on, a core mod you remove stays removed
        save_state()
    return failed


def resolve(link: str) -> tuple[Source, bool]:
    """A link, or a bare mod name like "Cryptid"; returns (source, whether it was found by name)."""
    try:
        return parse_source(link), False
    except Fail:
        if not re.fullmatch(r"[\w .'+-]+", link.strip()):
            raise
    src = find_by_name(link.strip())
    if src is None:
        die(f"couldn't find a mod called {link.strip()!r}; give its GitHub link")
    return src, True


def cmd_install(links: list[str], channel: str, name: str | None) -> int:
    if name and len(links) > 1:
        die("--name only works with one link")
    picks = []
    for link in links:  # bad links fail before anything changes
        src, by_name = resolve(link)
        picks.append((canonical_name(name or key_for(src) or src.name), src, by_name))

    def install(n: str, src: Source, by_name: bool) -> Result:
        if by_name:
            note(f"from {display_source(src.url)}")
        return ensure_mod(src, channel, n)

    failed = run_jobs([(n, lambda p=(n, s, b): install(*p)) for n, s, b in picks])
    for n, _, _ in picks:
        if n in STATE["mods"]:
            for w in missing_deps(n):
                warn(w)
    return failed


def cmd_update(channel: str | None) -> int:
    if not STATE.get("setup"):  # never set up: setting up is the update
        rest = [n for n in STATE["mods"] if not is_core(n)]
        width = max(len(n) for n in ["lovely", "balala", *map(core_name, CORE), *rest])  # one column for both
        failed = cmd_setup(channel, width)
        return failed + run_jobs([*((n, _mod_job(n, channel)) for n in rest), ("balala", ensure_self)], width)
    core = [n for n in map(core_name, CORE) if n in STATE["mods"]]
    names = [*core, *(n for n in STATE["mods"] if n not in core)]
    jobs = [("lovely", ensure_lovely), *((n, _mod_job(n, channel)) for n in names), ("balala", ensure_self)]
    return run_jobs(jobs)


def cmd_list() -> int:
    rows = [("lovely", "release", describe(STATE.get("lovely")) or "not installed", LOVELY_REPO)]
    core = {n: core_name(n) for n in CORE}
    others = sorted((k for k in STATE["mods"] if k not in core.values()), key=str.lower)
    for n in [*core.values(), *others]:
        rec = STATE["mods"].get(n)
        if rec is None:
            rows.append((n, "", "not installed", CORE[n]))
            continue
        version = describe(rec) + ("" if (ENV.mods / n).is_dir() else " (folder missing)")
        rows.append((n, rec["channel"] if rec["kind"] != "archive" else "file", version, display_source(rec["source"])))
    for d in mod_folders():
        if d.name not in STATE["mods"]:
            what = f"a second copy of {_dupes[d.name]}" if d.name in _dupes else "not managed: source unknown"
            rows.append((d.name, "", what, ""))
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
        core = is_core(name)
        delete(ENV.mods / name)
        STATE["mods"].pop(name, None)
        save_state()
        if core:
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
        scan(tidy=True)
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
  balala install Cryptid                  install a mod by name
  balala install SpectralPack/Cryptid -s  ...or by repo, at its newest release
  balala update                           update everything, balala included
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
        help="install mods by name or link",
        description="Install mods. Give a mod's name (looked up on Thunderstore, then GitHub), "
        "a GitHub repo (owner/repo, a URL, or a .../tree/<branch> URL), any git URL, a .zip/.tar.gz URL, "
        "or a local archive or git checkout. Installing an installed mod updates it.",
    )
    sp.add_argument("links", nargs="+", metavar="MOD")
    sp.add_argument("-n", "--name", help="folder name in Mods/ (default: the repo's name)")
    channels(sp, True)
    sp = sub.add_parser(
        "update",
        help="update lovely, Steamodded, Amulet, your mods and balala",
        description="Update lovely, Steamodded, Amulet, every mod (including ones installed by hand), and "
        "balala itself. Each mod stays on its channel unless you pass -l or -s, which moves them all; "
        "lovely and balala always move to their newest release.",
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
        if cmd == "logs":
            return cmd_logs(args.follow, args.lines)
        if cmd in ("list", "setup", "install", "update"):
            scan(tidy=cmd != "list")  # take over mods installed by hand
        if cmd == "list":
            return cmd_list()
        if cmd == "setup":
            failed = cmd_setup(args.channel or "latest")
        elif cmd == "install":
            failed = cmd_install(args.links, args.channel or "latest", args.name)
        elif cmd == "update":
            failed = cmd_update(args.channel)
        else:
            failed = cmd_remove(args.names)
        for w in conflicts():
            warn(w)
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
