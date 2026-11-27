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
    assert same_version_new_commit.current_label == "0.5.0-beta+aaaaaaa"
    assert same_version_new_commit.latest_label == "0.5.0-beta+bbbbbbb"
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
