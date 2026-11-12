from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import balala

PLATFORM = balala.current_platform()


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def sandbox(tmp_path, monkeypatch, request):
    """A fake game folder, Mods folder and data folder; nothing real is touched."""
    game = tmp_path / "game"
    game.mkdir()
    marker = game / PLATFORM.marker
    marker.mkdir() if marker.suffix == ".app" else marker.write_bytes(b"")
    env = {
        "BALALA_GAME_DIR": game,
        "BALALA_MODS_DIR": tmp_path / "Mods",
        "BALALA_DATA_DIR": tmp_path / "data",
        "NO_COLOR": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("BALALA_STEAM_DIR", raising=False)
    monkeypatch.setattr(balala, "game_running", lambda: False)
    if "network" not in request.keywords:  # offline tests never fetch lovely
        monkeypatch.setattr(balala, "ensure_lovely", lambda: balala.Result("current", "v0.0.0"))
    launches = []
    monkeypatch.setattr(type(PLATFORM), "launch", lambda self, g: launches.append(g))
    return SimpleNamespace(root=tmp_path, game=game, mods=tmp_path / "Mods", data=tmp_path / "data", launches=launches)


def run(*args: str) -> int:
    return balala.main(list(args))


def state(sb) -> dict:
    return json.loads((sb.data / "state.json").read_text())


def seed_setup(sb) -> None:
    """Pretend setup already ran, so offline tests don't fetch lovely and friends."""
    for f in PLATFORM.lovely_default_files():
        (sb.game / f).write_bytes(b"")
    sb.data.mkdir(parents=True, exist_ok=True)
    (sb.data / "state.json").write_text(
        json.dumps({"setup": "x", "lovely": {"ref": "v0.0.0", "files": PLATFORM.lovely_default_files()}, "mods": {}})
    )


class Upstream:
    """A bare repo standing in for a mod's GitHub repo."""

    def __init__(self, root: Path, header: dict | None = None):
        self.bare = root / "upstream.git"
        self.work = root / "work"
        sh("init", "-q", "--bare", "-b", "main", str(self.bare))
        sh("init", "-q", "-b", "main", str(self.work))
        sh("remote", "add", "origin", self.bare.as_uri(), cwd=self.work)
        (self.work / "toy.json").write_text(json.dumps(header or {"id": "Toy", "main_file": "toy.lua"}))
        self.commit("v1")
        sh("push", "-q", "-u", "origin", "main", cwd=self.work)

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(self, text: str, amend: bool = False) -> None:
        (self.work / "toy.lua").write_text(text)
        sh("add", "-A", cwd=self.work)
        sh("commit", "-q", *(["--amend"] if amend else []), "-m", text, cwd=self.work)

    def push(self, force: bool = False, branch: str = "main") -> None:
        sh("push", "-q", *(["-f"] if force else []), "origin", branch, cwd=self.work)

    def branch(self, name: str) -> None:
        sh("switch", "-q", "-c", name, cwd=self.work)
        self.commit(name)
        self.push(branch=name)
        sh("switch", "-q", "main", cwd=self.work)


def head_of(clone: Path) -> tuple[str, str]:
    return sh("rev-parse", "--abbrev-ref", "HEAD", cwd=clone), sh("log", "-1", "--format=%s", cwd=clone)


def init_globals() -> None:
    """Set up balala's ENV and STATE so tests can call its internals."""
    balala.ENV = balala.Env()
    balala.STATE = balala.load_state()


# ── parsing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "link, kind, name, url, branch",
    [
        ("SpectralPack/Cryptid", "github", "Cryptid", "https://github.com/SpectralPack/Cryptid", None),
        (
            "https://github.com/SpectralPack/Cryptid",
            "github",
            "Cryptid",
            "https://github.com/SpectralPack/Cryptid",
            None,
        ),
        ("github.com/SpectralPack/Cryptid.git", "github", "Cryptid", "https://github.com/SpectralPack/Cryptid", None),
        ("https://www.github.com/a/b/", "github", "b", "https://github.com/a/b", None),
        ("git@github.com:a/b.git", "github", "b", "https://github.com/a/b", None),
        ("https://github.com/a/b/tree/feature/x", "github", "b", "https://github.com/a/b", "feature/x"),
        ("https://gitlab.com/someone/mod.git", "git", "mod", "https://gitlab.com/someone/mod.git", None),
        ("git@codeberg.org:someone/mod.git", "git", "mod", "git@codeberg.org:someone/mod.git", None),
        (
            "https://example.com/files/Cool-Mod.zip",
            "archive",
            "Cool-Mod",
            "https://example.com/files/Cool-Mod.zip",
            None,
        ),
        (
            "https://github.com/frostice482/amulet/releases/download/3.6.1/Amulet.zip",
            "archive",
            "amulet",
            "https://github.com/frostice482/amulet/releases/download/3.6.1/Amulet.zip",
            None,
        ),
    ],
)
def test_parse_source(link, kind, name, url, branch):
    src = balala.parse_source(link)
    assert (src.kind, src.name, src.url, src.branch) == (kind, name, url, branch)


@pytest.mark.parametrize("link", ["gitlab.com/foo", "not a link", "https://github.com/onlyowner"])
def test_parse_source_rejects(link):
    with pytest.raises(balala.Fail):
        balala.parse_source(link)


def test_parse_source_local(tmp_path):
    archive = tmp_path / "Thing.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("thing.lua", "")
    src = balala.parse_source(str(archive))
    assert (src.kind, src.name) == ("archive", "Thing")
    assert src.url.startswith("file://")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(balala.Fail, match="not a git repo"):
        balala.parse_source(str(plain))


def test_same_remote():
    assert balala.same_remote("git@github.com:A/B.git", "https://www.github.com/a/b/")
    assert not balala.same_remote("https://github.com/a/b", "https://github.com/a/c")
    assert not balala.same_remote(None, "https://github.com/a/b")


def test_release_archive_url():
    one = {"tag_name": "1.0", "assets": [{"name": "Amulet.zip", "browser_download_url": "u1"}]}
    assert balala.release_archive_url("o/amulet", one) == "u1"
    two = {
        "tag_name": "1.0",
        "assets": [
            {"name": "amulet-full.zip", "browser_download_url": "u1"},
            {"name": "textures.zip", "browser_download_url": "u2"},
        ],
    }
    assert balala.release_archive_url("o/amulet", two) == "u1"
    none = {"tag_name": "1.0~beta/x", "assets": []}
    assert balala.release_archive_url("o/r", none) == "https://codeload.github.com/o/r/zip/refs/tags/1.0~beta%2Fx"


def test_transition():
    assert balala.transition("main@aaaaaaa", "main@bbbbbbb") == "main@aaaaaaa → bbbbbbb"
    assert balala.transition("v1", "main@bbbbbbb") == "v1 → main@bbbbbbb"
    assert balala.transition("", "v2") == "v2"


# ── Steam ─────────────────────────────────────────────────────────────────


def test_parse_vdf():
    text = r"""
    "libraryfolders"
    {
        "0" { "path" "C:\\Program Files (x86)\\Steam" "apps" { "228980" "1" } }
        "1" { "Path" "D:\\Games \"Steam\"" "apps" { "2379780" "123" } }
    }"""
    d = balala.parse_vdf(text)
    assert d["libraryfolders"]["0"]["path"] == r"C:\Program Files (x86)\Steam"
    assert d["libraryfolders"]["1"]["path"] == 'D:\\Games "Steam"'
    assert "2379780" in d["libraryfolders"]["1"]["apps"]


def vdf_path(p: Path) -> str:
    return str(p).replace("\\", "\\\\")


def test_find_game_via_libraryfolders(tmp_path, monkeypatch):
    steam, other = tmp_path / "Steam", tmp_path / "Library2"
    (steam / "steamapps").mkdir(parents=True)
    game = other / "steamapps/common/Balatro"
    game.mkdir(parents=True)
    marker = game / PLATFORM.marker
    marker.mkdir() if marker.suffix == ".app" else marker.write_bytes(b"")
    first, second = vdf_path(steam), vdf_path(other)
    (steam / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{first}" }} "1" {{ "path" "{second}" "apps" {{ "2379780" "1" }} }} }}'
    )
    monkeypatch.setenv("BALALA_STEAM_DIR", str(steam))
    monkeypatch.delenv("BALALA_GAME_DIR", raising=False)
    found = balala.find_game(PLATFORM)
    assert found.dir == game
    assert found.library == other


@pytest.mark.parametrize(
    "options, ok",
    [
        ('WINEDLLOVERRIDES="winmm=n,b" %command%', True),
        ("WINEDLLOVERRIDES=winmm=n,b %command%", True),
        ('WINEDLLOVERRIDES="version,winmm=n,b" %command%', True),
        ('PROTON_LOG=1 WINEDLLOVERRIDES="d3d11=n;winmm=n" %command% -fullscreen', True),
        ('WINEDLLOVERRIDES="winmm=b" %command%', False),  # builtin: lovely never loads
        ('WINEDLLOVERRIDES="winmm=n,b"', False),  # no %command%: Steam ignores it
        ("PROTON_LOG=1 %command%", False),
    ],
)
def test_loads_native(options, ok):
    assert balala.loads_native(options, ["winmm"]) is ok


def test_launch_options_of_the_signed_in_account(tmp_path, monkeypatch):
    steam = tmp_path / "Steam"
    monkeypatch.setenv("BALALA_STEAM_DIR", str(steam))
    for account, opts in (("11", 'WINEDLLOVERRIDES=\\"winmm=n,b\\" %command%'), ("22", "%command%")):
        cfg = steam / "userdata" / account / "config/localconfig.vdf"
        cfg.parent.mkdir(parents=True)
        apps = f'"apps" {{ "2379780" {{ "LaunchOptions" "{opts}" }} }}'
        cfg.write_text(f'"UserLocalConfigStore" {{ "Software" {{ "Valve" {{ "Steam" {{ {apps} }} }} }} }}')
    (steam / "config").mkdir()

    def sign_in(account: int) -> None:
        steam_id = 76561197960265728 + account
        (steam / "config/loginusers.vdf").write_text(f'"users" {{ "{steam_id}" {{ "MostRecent" "1" }} }}')

    game = balala.Game(tmp_path / "game", tmp_path)
    sign_in(22)
    assert balala.Linux().hints(game, ["winmm.dll"])  # account 22 hasn't set the option
    sign_in(11)
    assert balala.Linux().hints(game, ["winmm.dll"]) == []


def test_linux_prefix_and_launch_options(tmp_path, monkeypatch):
    linux = balala.Linux()
    lib = tmp_path / "lib"
    env = SimpleNamespace(game=balala.Game(lib / "steamapps/common/Balatro", lib))
    with pytest.raises(balala.Fail, match="launch it once from Steam"):
        linux.mods_dir(env)
    pfx = lib / "steamapps/compatdata/2379780/pfx"
    pfx.mkdir(parents=True)
    assert linux.mods_dir(env) == pfx / "drive_c/users/steamuser/AppData/Roaming/Balatro/Mods"

    steam = tmp_path / "Steam"
    monkeypatch.setenv("BALALA_STEAM_DIR", str(steam))
    (hint,) = linux.hints(env.game, ["winmm.dll"])
    assert 'WINEDLLOVERRIDES="winmm=n,b" %command%' in hint
    cfg = steam / "userdata/123/config/localconfig.vdf"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { "2379780" '
        '{ "LaunchOptions" "WINEDLLOVERRIDES=\\"winmm=n,b\\" %command%" } } } } } }'
    )
    assert linux.hints(env.game, ["winmm.dll"]) == []


# ── mods from git ─────────────────────────────────────────────────────────


def test_git_install_update_and_local_work(sandbox, tmp_path, capsys):
    seed_setup(sandbox)
    up = Upstream(tmp_path)
    assert run("install", up.url, "-n", "toy") == 0
    clone = sandbox.mods / "toy"
    assert (clone / "toy.json").is_file()
    assert state(sandbox)["mods"]["toy"]["kind"] == "git"

    up.commit("v2")
    up.push()
    assert run("update") == 0
    assert (clone / "toy.lua").read_text() == "v2"

    up.commit("v2b", amend=True)  # upstream rewrites history; the clone is untouched, so follow it
    up.push(force=True)
    capsys.readouterr()
    assert run("update") == 0
    assert "rewrote its history" in capsys.readouterr().out
    assert (clone / "toy.lua").read_text() == "v2b"

    (clone / "toy.lua").write_text("my edit")  # local work is never discarded
    up.commit("v3")
    up.push()
    assert run("update") == 0
    assert "local changes" in capsys.readouterr().out
    assert (clone / "toy.lua").read_text() == "my edit"

    assert run("remove", "toy") == 0  # ...not even by remove: it goes to backups
    assert not clone.exists()
    (backup,) = (sandbox.data / "backups").iterdir()
    assert (backup / "toy.lua").read_text() == "my edit"


def test_kept_local_commit_survives_later_updates(sandbox, tmp_path, capsys):
    seed_setup(sandbox)
    up = Upstream(tmp_path)
    run("install", up.url, "-n", "toy")
    clone = sandbox.mods / "toy"
    sh("commit", "-q", "--allow-empty", "-m", "mine", cwd=clone)
    up.commit("v1b", amend=True)  # upstream diverges from the local commit
    up.push(force=True)
    for _ in range(2):  # the second update must not take the kept commit for balala's own
        assert run("update") == 0
        assert "local changes or commits" in capsys.readouterr().out
        assert head_of(clone)[1] == "mine"
    assert run("remove", "toy") == 0
    (backup,) = (sandbox.data / "backups").iterdir()
    assert head_of(backup)[1] == "mine"


@pytest.mark.parametrize("ignored", [False, True])
def test_untracked_and_ignored_files_are_preserved(sandbox, tmp_path, ignored):
    up = Upstream(tmp_path)
    run("install", up.url, "-n", "toy")
    clone = sandbox.mods / "toy"
    if ignored:
        (clone / ".git/info/exclude").write_text("settings.lua\n")
    (clone / "settings.lua").write_text("mine")
    assert run("remove", "toy") == 0
    (backup,) = (sandbox.data / "backups").iterdir()
    assert (backup / "settings.lua").read_text() == "mine"


def test_branch_link_and_back(sandbox, tmp_path):
    up = Upstream(tmp_path)
    up.branch("dev")
    init_globals()
    balala.ensure_mod(balala.Source(up.url, "git", "toy", branch="dev"), "latest")
    assert head_of(sandbox.mods / "toy") == ("dev", "dev")
    balala.ensure_mod(balala.Source(up.url, "git", "toy"), "latest")  # asking for the default branch switches back
    assert head_of(sandbox.mods / "toy") == ("main", "v1")
    assert not (sandbox.data / "backups").exists()  # the dev clone was balala's and untouched


def test_case_variant_names_share_one_record(sandbox, tmp_path):
    up = Upstream(tmp_path)
    run("install", up.url, "-n", "toy")
    run("install", up.url, "-n", "TOY")
    assert list(state(sandbox)["mods"]) == ["toy"]


def test_remove_clean_clone_deletes_it(sandbox, tmp_path):
    up = Upstream(tmp_path)
    run("install", up.url, "-n", "toy")
    assert run("remove", "TOY") == 0  # names match case-insensitively
    assert not (sandbox.mods / "toy").exists()
    assert not (sandbox.data / "backups").exists()
    assert "toy" not in state(sandbox)["mods"]


def test_update_keeps_each_mods_channel(sandbox, tmp_path, monkeypatch):
    seed_setup(sandbox)
    up = Upstream(tmp_path)
    run("install", up.url, "-n", "toy")
    st = state(sandbox)
    st["mods"]["toy"]["channel"] = "stable"  # as if installed with -s
    (sandbox.data / "state.json").write_text(json.dumps(st))
    seen = []
    monkeypatch.setattr(
        balala, "ensure_mod", lambda src, ch, name=None: seen.append((name, ch)) or balala.Result("current")
    )
    run("update")
    assert seen == [("toy", "stable")]
    seen.clear()
    run("update", "-l")
    assert seen == [("toy", "latest")]


def test_update_before_setup_keeps_existing_core_channels(sandbox, monkeypatch):
    sandbox.data.mkdir()
    rec = {"source": "https://github.com/Steamodded/smods", "channel": "stable", "kind": "release", "ref": "1.0"}
    (sandbox.data / "state.json").write_text(json.dumps({"mods": {"smods": rec}}))
    seen = []
    monkeypatch.setattr(
        balala, "ensure_mod", lambda src, ch, name=None: seen.append((name, ch)) or balala.Result("current")
    )
    assert run("update") == 0
    assert seen == [("smods", "stable"), ("amulet", "latest")]


def test_filesystem_errors_fail_one_job_not_the_run(sandbox, monkeypatch, capsys):
    seed_setup(sandbox)
    st = state(sandbox)
    st["mods"] = {
        n: {"source": f"https://github.com/o/{n}", "channel": "latest", "kind": "git", "ref": "0" * 40}
        for n in ("one", "two")
    }
    (sandbox.data / "state.json").write_text(json.dumps(st))
    seen = []

    def flaky(src, ch, name=None):
        seen.append(name)
        if name == "one":
            raise PermissionError(13, "Permission denied", "/some/file.lua")
        return balala.Result("current")

    monkeypatch.setattr(balala, "ensure_mod", flaky)
    assert run("update") == 1
    out = capsys.readouterr().out
    assert "✗ one" in out and "Permission denied" in out
    assert seen == ["one", "two"]


# ── mods from archives, folders and headers ───────────────────────────────


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return path


def test_archive_install_and_update(sandbox, tmp_path, capsys):
    archive = make_zip(tmp_path / "Thing.zip", {"Thing/thing.lua": "v1"})  # one top folder: unwrapped
    assert run("install", str(archive)) == 0
    assert (sandbox.mods / "Thing/thing.lua").read_text() == "v1"
    capsys.readouterr()
    assert run("install", str(archive)) == 0
    assert "✓" in capsys.readouterr().out
    make_zip(archive, {"thing.lua": "v2"})  # flat zips work too
    assert run("install", str(archive)) == 0
    assert (sandbox.mods / "Thing/thing.lua").read_text() == "v2"


def test_edited_archive_install_is_backed_up(sandbox, tmp_path):
    archive = make_zip(tmp_path / "Thing.zip", {"thing.lua": "v1"})
    run("install", str(archive))
    (sandbox.mods / "Thing/thing.lua").write_text("my tweak")
    make_zip(archive, {"thing.lua": "v2"})
    assert run("install", str(archive)) == 0
    assert (sandbox.mods / "Thing/thing.lua").read_text() == "v2"
    (backup,) = (sandbox.data / "backups").iterdir()
    assert (backup / "thing.lua").read_text() == "my tweak"


def test_same_archive_from_a_new_link_updates_the_source(sandbox, tmp_path):
    old = make_zip(tmp_path / "old.zip", {"thing.lua": "v1"})
    new = tmp_path / "new.zip"
    new.write_bytes(old.read_bytes())
    run("install", str(old), "-n", "Thing")
    run("install", str(new), "-n", "Thing")
    assert state(sandbox)["mods"]["Thing"]["source"] == new.resolve().as_uri()


def test_unmanaged_folder_is_listed_and_backed_up(sandbox, capsys):
    hand = sandbox.mods / "HandMade"
    hand.mkdir(parents=True)
    (hand / "main.lua").write_text("mine")
    seed_setup(sandbox)
    run("list")
    assert "not managed by balala" in capsys.readouterr().out
    assert run("remove", "handmade") == 0
    (backup,) = (sandbox.data / "backups").iterdir()
    assert (backup / "main.lua").read_text() == "mine"


def test_dependency_warnings(sandbox, tmp_path, capsys):
    header = {"id": "Toy", "main_file": "toy.lua", "dependencies": ["Talisman | Other", "Steamodded (>=1.0)"]}
    up = Upstream(tmp_path, header)
    run("install", up.url, "-n", "toy")
    err = capsys.readouterr().err
    assert "Talisman | Other" in err and "Steamodded (>=1.0)" in err
    provider = sandbox.mods / "amulet"
    provider.mkdir()
    (provider / "smods.json").write_text(json.dumps({"id": "Amulet", "main_file": "x.lua", "provides": ["Talisman"]}))
    assert balala.missing_deps("toy") == ["Toy needs Steamodded (>=1.0), which isn't installed"]


@pytest.mark.parametrize("name", ["lovely", ".hidden", "a/b", ".."])
def test_bad_folder_names(sandbox, tmp_path, name):
    up = Upstream(tmp_path)
    assert run("install", up.url, "-n", name) == 1


# ── launch and logs ───────────────────────────────────────────────────────


def test_launch(sandbox, capsys, monkeypatch):
    seed_setup(sandbox)
    assert run() == 0
    assert sandbox.launches == [balala.Game(sandbox.game, None)]
    assert "Balatro is starting" in capsys.readouterr().out
    monkeypatch.setattr(balala, "game_running", lambda: True)
    assert run("launch") == 0
    assert "already running" in capsys.readouterr().out
    assert len(sandbox.launches) == 1


def test_logs(sandbox, capsys):
    seed_setup(sandbox)
    assert run("logs") == 1
    logs = sandbox.mods / "lovely/log"
    logs.mkdir(parents=True)
    (logs / "lovely-1.log").write_text("\n".join(f"line {i}" for i in range(100)))
    capsys.readouterr()
    assert run("logs", "-n", "3") == 0
    out = capsys.readouterr().out
    assert "line 99" in out and "line 96" not in out


# ── against GitHub ────────────────────────────────────────────────────────


@pytest.mark.network
def test_setup_install_update_against_github(sandbox, capsys):
    assert run("setup", "--stable") == 0
    st = state(sandbox)
    assert all((sandbox.game / f).is_file() for f in st["lovely"]["files"])
    assert {n: (r["channel"], r["kind"]) for n, r in st["mods"].items()} == {
        "smods": ("stable", "release"),
        "amulet": ("stable", "release"),
    }
    assert (sandbox.mods / "amulet/smods.json").is_file()  # Amulet's flat release zip, unpacked in place

    assert run("install", "SpectralPack/Cryptid") == 0
    assert balala.missing_deps("Cryptid") == []  # Cryptid wants Amulet and Steamodded
    capsys.readouterr()
    assert run("update") == 0  # nothing changed, and each mod kept its channel
    out = capsys.readouterr().out
    assert out.count("✓") == 4, out
    assert state(sandbox)["mods"]["smods"]["kind"] == "release"

    assert run("update", "--latest") == 0
    assert state(sandbox)["mods"]["smods"]["kind"] == "git"
    assert run("list") == 0


@pytest.mark.network
def test_missing_github_repo(sandbox, capsys):
    assert run("install", "a9lim/this-repo-does-not-exist-balala") == 1
    assert "no such GitHub repo" in capsys.readouterr().out
