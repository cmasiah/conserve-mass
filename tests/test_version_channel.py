from app.services.version import VersionInfo


def test_stable_compares_versions():
    v = VersionInfo(current="0.4.3", latest="0.5.0", channel="stable", image_ready=True)
    assert v.newer and v.update_available and v.latest_label == "0.5.0"
    assert not VersionInfo(current="0.5.0", latest="0.5.0", channel="stable").newer


def test_beta_compares_commits_not_versions():
    a, b = "a" * 40, "b" * 40
    same_version_new_commit = VersionInfo(current="0.5.0", latest="0.5.0", channel="beta",
                                          current_sha=a, latest_sha=b, image_ready=False)
    assert same_version_new_commit.newer and same_version_new_commit.building
    # Plain numbers, no commit hashes: the running build knows its own number, and the
    # newest one is named from the v0.5.N-beta tag CI put on its commit.
    assert same_version_new_commit.current_label == "0.5.0 beta"
    assert same_version_new_commit.latest_label == "the newest beta"
    same_version_new_commit.sha_versions = {b: "0.5.1"}
    assert same_version_new_commit.latest_label == "0.5.1 beta"
    assert not VersionInfo(current="0.5.0", latest="0.5.0", channel="beta",
                           current_sha=a, latest_sha=a).newer
    # A local build without a commit never shows an update.
    assert not VersionInfo(current="0.5.0", latest="0.6.0", channel="beta",
                           current_sha="", latest_sha=b).newer


def test_only_new_main_releases_are_announced():
    from app.services.version import release_to_announce
    assert release_to_announce("0.5.0", True, None) is None        # first run: baseline
    assert release_to_announce("0.5.0", True, "0.5.0") is None     # nothing new
    assert release_to_announce("0.6.0", False, "0.5.0") is None    # image not built yet
    assert release_to_announce("0.6.0", True, "0.5.0") == "0.6.0"
    assert release_to_announce("0.10.0", True, "0.9.2") == "0.10.0"


def test_beta_commits_never_ping(db, monkeypatch):
    import asyncio
    from app.models import Notification, Setting
    from app.services import version
    db.add(Setting(key=version.RELEASE_SEEN_KEY, value="0.5.0"))
    db.commit()

    async def fake_check(force=False):
        return version.VersionInfo(channel="beta", current_sha="a" * 40,
                                   latest_sha="b" * 40, latest="0.5.0", image_ready=True)

    async def same_release():
        return "0.5.0", True
    monkeypatch.setattr(version, "check", fake_check)
    monkeypatch.setattr(version, "main_release", same_release)
    asyncio.run(version.version_job())
    assert db.query(Notification).count() == 0       # new beta commit: no DM

    async def promoted():
        return "0.6.0", True
    monkeypatch.setattr(version, "main_release", promoted)
    asyncio.run(version.version_job())
    asyncio.run(version.version_job())                # still only one DM
    db.expire_all()
    notes = db.query(Notification).all()
    assert [n.title for n in notes] == ["🚀 Conserve Mass Ops 0.6.0 released"]
    assert db.get(Setting, version.RELEASE_SEEN_KEY).value == "0.6.0"


def test_rollback_tags_are_strict():
    from app.services.version import valid_rollback_tag
    assert valid_rollback_tag("sha-" + "0123456789abcdef" * 2 + "01234567")
    assert valid_rollback_tag("0.5.0")
    for bad in ("beta", "latest", "sha-abc", "0.5", "0.5.0;reboot", "", "SHA-" + "a" * 40):
        assert not valid_rollback_tag(bad), bad


def test_recent_builds_beta_only_lists_published_commits(monkeypatch):
    import asyncio
    from app.services import version
    monkeypatch.setattr(version, "CHANNEL", "beta")
    monkeypatch.setattr(version, "GIT_SHA", "b" * 40)
    commits = [{"sha": "b" * 7, "full_sha": "b" * 40, "message": "new", "date": None, "url": "u"},
               {"sha": "c" * 7, "full_sha": "c" * 40, "message": "no image", "date": None,
                "url": "u"},
               {"sha": "a" * 7, "full_sha": "a" * 40, "message": "old", "date": None, "url": "u"}]

    async def fake_tags(client, repo):
        return ["beta", "sha-" + "a" * 40, "sha-" + "b" * 40, "0.5.0"]
    monkeypatch.setattr(version, "_ghcr_tags", fake_tags)
    builds = asyncio.run(version.recent_builds(None, "o/r", commits))
    assert [b["message"] for b in builds] == ["new", "old"]
    assert [b["kind"] for b in builds] == ["current", "rollback"]


def test_rollback_route_validates_and_writes_the_request(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from app.main import app
    from app.services import updater, version
    monkeypatch.setattr(updater, "_dir", lambda: tmp_path)
    (tmp_path / "updater-heartbeat").write_text("watcher=2\n")
    good = "sha-" + "a" * 40
    version._cache.builds = [{"tag": good, "label": "aaaaaaa", "message": "", "date": None,
                              "url": None, "current": False}]

    async def login(request: Request):
        request.session["user"] = {"character_id": 1, "name": "Boss"}
        request.session["csrf"] = "t"
        return {}
    app.add_api_route("/__test/login5", login)
    try:
        c = TestClient(app)
        c.get("/__test/login5")
        r = c.post("/system/rollback", data={"csrf": "t", "tag": "sha-" + "f" * 40},
                   follow_redirects=False)            # not in the list
        assert r.headers["location"] == "/system"
        assert not (tmp_path / "update-requested").exists()
        r = c.post("/system/rollback", data={"csrf": "t", "tag": good}, follow_redirects=False)
        assert r.headers["location"] == "/system?updating=1"
        assert f"tag={good}" in (tmp_path / "update-requested").read_text()
    finally:
        version._cache.builds = []
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", "") != "/__test/login5"]


def test_build_list_shows_one_upgrade_the_running_build_and_three_rollbacks():
    from app.services.version import trim_builds
    b = [{"tag": f"t{i}", "current": i == 2} for i in range(8)]   # newest first; running = 2
    out = trim_builds(b)
    assert [(x["tag"], x["kind"]) for x in out] == [
        ("t0", "upgrade"), ("t2", "current"), ("t3", "rollback"), ("t4", "rollback"),
        ("t5", "rollback")]                     # t1 (also newer) is left out
    b[0]["current"], b[2]["current"] = True, False      # running the newest: no upgrade row
    assert [x["kind"] for x in trim_builds(b)] == ["current", "rollback", "rollback", "rollback"]
    for x in b:
        x["current"] = False                            # running build not listed
    assert [x["kind"] for x in trim_builds(b)] == ["upgrade", "rollback", "rollback", "rollback"]



def test_beta_numbers_from_ci_tags_and_build_labels():
    import asyncio

    from app.services import version

    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data

        def json(self):
            return self._d

    class Client:
        async def get(self, url, params=None, headers=None):
            if url.endswith("/tags"):
                return R(200, [{"name": "v0.7.2-beta", "commit": {"sha": "B" * 40}},
                               {"name": "v0.7.1-beta", "commit": {"sha": "a" * 40}},
                               {"name": "v0.7.0", "commit": {"sha": "c" * 40}},
                               {"name": "random", "commit": {"sha": "d" * 40}}])
            return R(404, {})
    nums = asyncio.run(version.beta_numbers(Client(), "o/r"))
    assert nums == {"b" * 40: "0.7.2", "a" * 40: "0.7.1"}


def test_build_version_from_the_image_wins(monkeypatch):
    import importlib

    import app.config as cfg
    monkeypatch.setenv("APP_BUILD_VERSION", "0.7.12")
    assert importlib.reload(cfg).APP_VERSION == "0.7.12"
    monkeypatch.delenv("APP_BUILD_VERSION")
    assert importlib.reload(cfg).APP_VERSION == "0.7.0"
