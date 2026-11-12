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
    monkeypatch.setattr(balala, "_catalog", None)
    monkeypatch.setattr(balala, "_repo_ids", {})
    monkeypatch.setattr(balala, "_dupes", {})
    if "network" not in request.keywords:  # offline tests never touch GitHub
        monkeypatch.setattr(balala, "ensure_lovely", lambda: balala.Result("current", "v0.0.0"))
        monkeypatch.setattr(balala, "catalog", lambda: [])
        monkeypatch.setattr(balala, "search_github", lambda name: [])
        monkeypatch.setattr(balala, "repo_ids", lambda repo: set())
        monkeypatch.setattr(balala, "guess_channel", lambda folder, src: "latest")
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


def init_globals() -> None:
    """Set up balala's ENV and STATE so tests can call its internals."""
    balala.ENV = balala.Env()
    balala.STATE = balala.load_state()


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


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return path


def hand_install(mods: Path, folder: str, header: dict) -> Path:
    """A mod unzipped into Mods/ by hand: no .git, no record in balala's state."""
    d = mods / folder
    d.mkdir(parents=True)
    (d / "mod.json").write_text(json.dumps({"main_file": "main.lua", **header}))
    (d / "main.lua").write_text("")
    return d


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
    archive = make_zip(tmp_path / "Thing.zip", {"thing.lua": ""})
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


@pytest.mark.parametrize(
    "folder, stem",
    [("Cryptid-main", "Cryptid"), ("Cryptid-0.5.17d", "Cryptid"), ("smods-1.0.0~BETA-0827c", "smods"), ("Foo", "Foo")],
)
def test_folder_stem(folder, stem):
    assert balala._folder_stem(folder) == stem


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
    monkeypatch.setenv("BALALA_STEAM_DIR", str(tmp_path / "Steam"))
    (hint,) = linux.hints(env.game, ["winmm.dll"])
    assert 'WINEDLLOVERRIDES="winmm=n,b" %command%' in hint


# ── mods from git ─────────────────────────────────────────────────────────


def test_git_install_and_update(sandbox, tmp_path, capsys):
    seed_setup(sandbox)
    up = Upstream(tmp_path)
    assert run("install", up.url, "-n", "toy") == 0
    clone = sandbox.mods / "toy"
    assert state(sandbox)["mods"]["toy"]["kind"] == "git"

    up.commit("v2")
    up.push()
    assert run("update") == 0
    assert (clone / "toy.lua").read_text() == "v2"

    sh("commit", "-q", "--allow-empty", "-m", "mine", cwd=clone)  # a local commit...
    up.commit("v2b", amend=True)  # ...and upstream rewrites its history
    up.push(force=True)
    capsys.readouterr()
    assert run("update") == 0
    assert "reset to match" in capsys.readouterr().out
    assert head_of(clone) == ("main", "v2b")

    (clone / "toy.lua").write_text("my edit")  # uncommitted edits are the one thing update leaves alone
    up.commit("v3")
    up.push()
    assert run("update") == 0
    assert "uncommitted changes" in capsys.readouterr().out
    assert (clone / "toy.lua").read_text() == "my edit"

    assert run("remove", "TOY") == 0  # names match case-insensitively
    assert not clone.exists()
    assert "toy" not in state(sandbox)["mods"]


def test_branch_link_and_back(sandbox, tmp_path):
    up = Upstream(tmp_path)
    up.branch("dev")
    init_globals()
    balala.ensure_mod(balala.Source(up.url, "git", "toy", branch="dev"), "latest")
    assert head_of(sandbox.mods / "toy") == ("dev", "dev")
    balala.ensure_mod(balala.Source(up.url, "git", "toy"), "latest")  # asking for the default branch switches back
    assert head_of(sandbox.mods / "toy") == ("main", "v1")


def test_case_variant_names_share_one_record(sandbox, tmp_path):
    up = Upstream(tmp_path)
    run("install", up.url, "-n", "toy")
    run("install", up.url, "-n", "TOY")
    assert list(state(sandbox)["mods"]) == ["toy"]


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


# ── mods from archives ────────────────────────────────────────────────────


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


def test_same_archive_from_a_new_link_updates_the_source(sandbox, tmp_path):
    old = make_zip(tmp_path / "old.zip", {"thing.lua": "v1"})
    new = tmp_path / "new.zip"
    new.write_bytes(old.read_bytes())
    run("install", str(old), "-n", "Thing")
    run("install", str(new), "-n", "Thing")
    assert state(sandbox)["mods"]["Thing"]["source"] == new.resolve().as_uri()


@pytest.mark.parametrize("name", ["lovely", ".hidden", "a/b", ".."])
def test_bad_folder_names(sandbox, tmp_path, name):
    up = Upstream(tmp_path)
    assert run("install", up.url, "-n", name) == 1


# ── mods installed by hand ────────────────────────────────────────────────


def test_hand_made_clone_is_adopted_and_updated(sandbox, tmp_path, capsys):
    seed_setup(sandbox)
    up = Upstream(tmp_path)
    sandbox.mods.mkdir()
    sh("clone", "-q", up.url, str(sandbox.mods / "Toy"))
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "found main@" in out
    rec = state(sandbox)["mods"]["Toy"]
    assert (rec["kind"], rec["channel"]) == ("git", "latest")
    up.commit("v2")
    up.push()
    assert run("update") == 0
    assert (sandbox.mods / "Toy/toy.lua").read_text() == "v2"


def test_hand_install_found_on_thunderstore_is_replaced_on_update(sandbox, tmp_path, monkeypatch, capsys):
    seed_setup(sandbox)
    up = Upstream(tmp_path, {"id": "Thing", "main_file": "toy.lua"})
    hand_install(sandbox.mods, "Thing-main", {"id": "Thing", "version": "1.0"})
    monkeypatch.setattr(balala, "catalog", lambda: [{"name": "Thing", "repo": up.url}])
    monkeypatch.setattr(balala, "_verified", lambda url, ids: balala.parse_source(url) if "thing" in ids else None)
    assert run("list") == 0
    assert "Thing-main  latest   1.0 (by hand)" in capsys.readouterr().out
    assert run("update") == 0  # superseded by a real install from its source
    rec = state(sandbox)["mods"]["Thing-main"]
    assert rec["kind"] == "git"
    assert head_of(sandbox.mods / "Thing-main") == ("main", "v1")


def test_hand_install_found_through_its_own_manifest(sandbox, monkeypatch):
    seed_setup(sandbox)
    d = hand_install(sandbox.mods, "Gem", {"id": "Gem"})
    (d / "manifest.json").write_text(json.dumps({"name": "Gem", "website_url": "https://github.com/someone/gem-mod"}))
    monkeypatch.setattr(balala, "repo_ids", lambda repo: {"gem"} if repo == "someone/gem-mod" else set())
    run("list")
    assert state(sandbox)["mods"]["Gem"]["source"] == "https://github.com/someone/gem-mod"


def test_namesake_is_not_adopted(sandbox, monkeypatch, capsys):
    seed_setup(sandbox)
    hand_install(sandbox.mods, "Thing", {"id": "Thing"})
    monkeypatch.setattr(balala, "catalog", lambda: [{"name": "Thing", "repo": "https://github.com/someone/Thing"}])
    monkeypatch.setattr(balala, "repo_ids", lambda repo: {"otherthing"})  # same name, different mod
    run("list")
    assert "not managed: source unknown" in capsys.readouterr().out
    assert "Thing" not in state(sandbox)["mods"]


def test_folder_without_a_header_is_left_alone(sandbox, monkeypatch):
    seed_setup(sandbox)
    (sandbox.mods / "Jokers").mkdir(parents=True)  # a lovely-only mod: nothing to check a match against
    (sandbox.mods / "Jokers/lovely.toml").write_text("")
    monkeypatch.setattr(balala, "catalog", lambda: [{"name": "Jokers", "repo": "https://github.com/a/Jokers"}])
    run("list")
    assert state(sandbox)["mods"] == {}


def test_hand_installed_steamodded_counts_as_core(sandbox, capsys):
    seed_setup(sandbox)
    smods = sandbox.mods / "Steamodded-main"
    for p in ("src", "lovely"):
        (smods / p).mkdir(parents=True)
    (smods / "version.lua").write_text("")
    init_globals()
    balala.scan(tidy=False)
    assert balala.core_name("smods") == "Steamodded-main"
    capsys.readouterr()
    balala.cmd_list()
    out = capsys.readouterr().out
    assert "Steamodded-main" in out and "smods " not in out


def test_second_copy_is_deleted(sandbox, tmp_path, capsys):
    seed_setup(sandbox)
    up = Upstream(tmp_path)
    run("install", up.url, "-n", "Toy")
    sh("clone", "-q", up.url, str(sandbox.mods / "Toy-copy"))
    capsys.readouterr()
    run("list")
    assert "a second copy of Toy" in capsys.readouterr().out
    assert run("update") == 0
    assert "deleted: a second copy of Toy" in capsys.readouterr().out
    assert not (sandbox.mods / "Toy-copy").exists()


def test_install_by_name(sandbox, tmp_path, monkeypatch, capsys):
    up = Upstream(tmp_path)
    monkeypatch.setattr(balala, "catalog", lambda: [{"name": "ToyMod", "repo": up.url}])
    monkeypatch.setattr(balala, "_verified", lambda url, ids: balala.parse_source(url) if "toymod" in ids else None)
    assert run("install", "Toy Mod") == 0
    out = capsys.readouterr().out
    assert "from " in out
    assert state(sandbox)["mods"]["upstream"]["source"] == up.url
    assert run("install", "nothing-like-this") == 1


def test_conflicts(sandbox):
    hand_install(sandbox.mods, "Amulet", {"id": "Amulet", "provides": ["Talisman"]})
    hand_install(sandbox.mods, "Talisman", {"id": "Talisman"})
    hand_install(sandbox.mods, "Foo", {"id": "Foo"})
    hand_install(sandbox.mods, "Foo-main", {"id": "Foo"})
    init_globals()
    assert sorted(balala.conflicts()) == [
        "Amulet replaces Talisman; remove it",
        "Foo is installed twice (Foo and Foo-main); remove one",
    ]


def test_guess_channel(tmp_path, monkeypatch):
    guess = balala.__dict__["guess_channel"]
    monkeypatch.setattr(balala, "api", lambda path: [{"tag_name": "v1.2.0"}])
    src = balala.parse_source("o/Thing")
    for folder, header, expected in [
        ("Thing-1.2.0", {}, "stable"),
        ("Thing", {"version": "1.2.0"}, "stable"),
        ("Thing-main", {"version": "1.3.0-dev"}, "latest"),
    ]:
        d = hand_install(tmp_path, folder, {"id": "Thing", **header})
        assert guess(d, src) == expected, folder


def test_dependency_warnings(sandbox, tmp_path, capsys):
    header = {"id": "Toy", "main_file": "toy.lua", "dependencies": ["Talisman | Other", "Steamodded (>=1.0)"]}
    up = Upstream(tmp_path, header)
    run("install", up.url, "-n", "toy")
    err = capsys.readouterr().err
    assert "Talisman | Other" in err and "Steamodded (>=1.0)" in err
    hand_install(sandbox.mods, "amulet", {"id": "Amulet", "provides": ["Talisman"]})
    assert balala.missing_deps("toy") == ["Toy needs Steamodded (>=1.0), which isn't installed"]


def test_unknown_folder_is_listed_and_removable(sandbox, capsys):
    seed_setup(sandbox)
    hand = sandbox.mods / "HandMade"
    hand.mkdir(parents=True)
    (hand / "main.lua").write_text("mine")
    run("list")
    assert "not managed: source unknown" in capsys.readouterr().out
    assert run("remove", "handmade") == 0
    assert not hand.exists()


# ── updating balala itself ────────────────────────────────────────────────


def test_upgrade_command(tmp_path):
    def which(tool):
        return f"/bin/{tool}"

    uv_tool, pipx_env, venv = tmp_path / "uvtool", tmp_path / "pipxenv", tmp_path / "venv"
    for d in (uv_tool, pipx_env, venv):
        d.mkdir()
    (uv_tool / "uv-receipt.toml").write_text("")
    (pipx_env / "pipx_metadata.json").write_text("{}")
    assert balala.upgrade_command(uv_tool, "uv", "S", which)[:4] == ["uv", "tool", "install", "--force"]
    assert balala.upgrade_command(pipx_env, "pip", "S", which)[:3] == ["pipx", "install", "--force"]
    assert balala.upgrade_command(venv, "uv", "S", which)[:3] == ["uv", "pip", "install"]
    assert balala.upgrade_command(venv, "pip", "S", which)[1:4] == ["-m", "pip", "install"]
    assert balala.upgrade_command(uv_tool, "uv", "S", lambda tool: None)[1:4] == ["-m", "pip", "install"]


def fake_release(monkeypatch, tmp_path, version: str) -> Path:
    new = tmp_path / "release" / "balala.py"
    new.parent.mkdir()
    new.write_text(f'__version__ = "{version}"\n')
    rel = {"tag_name": f"v{version}", "assets": [{"name": "balala.py", "browser_download_url": new.as_uri()}]}
    monkeypatch.setattr(balala, "newest_release", lambda repo: rel)
    return new


def test_single_file_updates_itself(tmp_path, monkeypatch):
    copy = tmp_path / "bin" / "balala.py"
    copy.parent.mkdir()
    copy.write_text('__version__ = "0.0.1"\n')
    monkeypatch.setattr(balala, "__file__", str(copy))
    new = fake_release(monkeypatch, tmp_path, "99.0.0")
    res = balala.ensure_self()
    assert (res.status, res.detail) == ("updated", f"{balala.__version__} → 99.0.0")
    assert copy.read_text() == new.read_text()


def test_no_self_update_when_current_or_a_checkout(tmp_path, monkeypatch):
    copy = tmp_path / "bin" / "balala.py"
    copy.parent.mkdir()
    copy.write_text("")
    monkeypatch.setattr(balala, "__file__", str(copy))
    fake_release(monkeypatch, tmp_path, balala.__version__)
    assert balala.ensure_self().status == "current"
    (copy.parent / ".git").mkdir()  # someone's checkout: theirs to update
    (tmp_path / "newer").mkdir()
    fake_release(monkeypatch, tmp_path / "newer", "99.0.0")
    res = balala.ensure_self()
    assert res.status == "current" and "git checkout" in res.detail
    assert copy.read_text() == ""


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
    assert out.count("✓") == 5, out  # lovely, smods, amulet, Cryptid, and balala itself
    assert state(sandbox)["mods"]["smods"]["kind"] == "release"

    assert run("update", "--latest") == 0
    assert state(sandbox)["mods"]["smods"]["kind"] == "git"


def unzip_from(url: str, mods: Path, tmp: Path) -> None:
    archive = tmp / "dl.zip"
    balala.download(url, archive)
    with zipfile.ZipFile(archive) as z:
        z.extractall(mods)


@pytest.mark.network
def test_hand_installs_are_taken_over(sandbox, tmp_path, capsys):
    """The way people install by hand: GitHub's 'Download ZIP' or a release zip, unzipped into Mods/."""
    sandbox.mods.mkdir()
    init_globals()
    unzip_from("https://codeload.github.com/SpectralPack/Cryptid/zip/refs/tags/v0.5.17d", sandbox.mods, tmp_path)
    unzip_from("https://codeload.github.com/Steamodded/smods/zip/refs/tags/26.1002.0", sandbox.mods, tmp_path)
    (sandbox.mods / "Amulet").mkdir()
    unzip_from(
        "https://github.com/frostice482/amulet/releases/download/3.6.1/Amulet.zip", sandbox.mods / "Amulet", tmp_path
    )
    assert sorted(d.name for d in sandbox.mods.iterdir()) == ["Amulet", "Cryptid-0.5.17d", "smods-26.1002.0"]

    assert run("list") == 0
    st = state(sandbox)["mods"]
    assert st["Cryptid-0.5.17d"]["source"] == "https://github.com/SpectralPack/Cryptid"  # via Thunderstore
    assert st["Amulet"]["source"] == "https://github.com/frostice482/amulet"  # via its manifest.json
    assert st["smods-26.1002.0"]["source"] == "https://github.com/Steamodded/smods"  # by its layout
    assert st["Cryptid-0.5.17d"]["channel"] == "stable"  # the folder names a release tag

    assert run("update") == 0  # setup hasn't run, so this sets up too, reusing the hand installs
    st = state(sandbox)["mods"]
    assert sorted(st) == ["Amulet", "Cryptid-0.5.17d", "smods-26.1002.0"]
    assert all(r["kind"] in ("git", "release") for r in st.values())
    assert sorted(d.name for d in sandbox.mods.iterdir()) == ["Amulet", "Cryptid-0.5.17d", "smods-26.1002.0"]


@pytest.mark.network
def test_install_by_name_against_github(sandbox):
    assert run("install", "Cryptid", "Amulet") == 0  # GitHub search alone would pick another "amulet"
    mods = state(sandbox)["mods"]
    assert mods["Cryptid"]["source"] == "https://github.com/SpectralPack/Cryptid"
    assert mods["amulet"]["source"] == "https://github.com/frostice482/amulet"


@pytest.mark.network
def test_missing_github_repo(sandbox, capsys):
    assert run("install", "a9lim/this-repo-does-not-exist-balala") == 1
    assert "no such GitHub repo" in capsys.readouterr().out
