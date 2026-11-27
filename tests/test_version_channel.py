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
