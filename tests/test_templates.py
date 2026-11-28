"""Render real pages through the app's template setup to catch template-level bugs."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS


def _render(name, **ctx):
    from app.web.templating import templates
    base = dict(request=NS(url=NS(path="/system"), query_params={}), csrf="t", flash=None,
                user={"character_id": 1, "name": "Pilot"})
    return templates.env.get_template(name).render(**base, **ctx)


def test_system_page_shows_running_updater():
    """Regression: the sidebar once set a template variable named `upd`, which replaced
    the System page's updater status, so it always said "not set up"."""
    now = datetime.now(timezone.utc)
    ver = NS(current="0.5.0", current_label="0.5.0", channel="beta", update_available=False,
             building=False, latest="0.5.0", checked_at=now, error=None,
             builds=[{"tag": "sha-" + "a" * 40, "label": "aaaaaaa", "message": "Old build",
                      "date": now - timedelta(days=1), "url": "https://x", "current": False,
                      "kind": "rollback"},
                     {"tag": "sha-" + "b" * 40, "label": "bbbbbbb", "message": "Newest",
                      "date": now, "url": "https://x", "current": True, "kind": "current"}])
    upd = NS(installed=True, watcher_version=2, last_seen=now - timedelta(seconds=20), pending=False,
             last_result=None, last_finished=None, last_started=None, log_tail="")
    settings = NS(base_url="http://x", sso_callback_url="http://x/cb", config_dir="/config",
                  contract_poll_seconds=60, esi_contact="a@b.c", spam_contracts_per_hour=10,
                  digest_threshold=3, discord_max_messages_per_hour=30,
                  max_location_lookups_per_sync=25, update_repo="o/r")
    html = _render("system.html", ver=ver, upd=upd, settings=settings, notes=[], syncs=[],
                   backups=[], bot_ready=True, bot_configured=True, now=now,
                   esi_usage=__import__("app.esi.usage", fromlist=["summary"]).summary(),
                   esi_expected=[])
    assert ">active<" in html
    assert "not set up" not in html
    assert "The host updater is not running" not in html      # button not disabled


def test_system_page_rollback_list_and_folded_notifications():
    import re
    from tests.test_templates import _render  # noqa
    now = datetime.now(timezone.utc)
    ver = NS(current="0.5.0", current_label="0.5.0", channel="beta", update_available=False,
             building=False, latest="0.5.0", checked_at=now, error=None,
             builds=[{"tag": "sha-" + "a" * 40, "label": "aaaaaaa", "message": "Old build",
                      "date": now, "url": "https://x", "current": False,
                      "kind": "rollback"}])
    settings = NS(base_url="http://x", sso_callback_url="http://x/cb", config_dir="/config",
                  contract_poll_seconds=60, esi_contact="a@b.c", spam_contracts_per_hour=10,
                  digest_threshold=3, discord_max_messages_per_hour=30,
                  max_location_lookups_per_sync=25, update_repo="o/r")
    from app.esi import usage
    for watcher, disabled in ((1, True), (2, False)):
        upd = NS(installed=True, watcher_version=watcher, last_seen=now, pending=False,
                 last_result=None, last_finished=None, last_started=None, log_tail="")
        html = _render("system.html", ver=ver, upd=upd, settings=settings, notes=[],
                       syncs=[], backups=[], bot_ready=True, bot_configured=True, now=now,
                       esi_usage=usage.summary(), esi_expected=[])
        btn = re.search(r'<button class="small"([^>]*)>Roll back to this</button>', html)
        assert btn and (("disabled" in btn.group(1)) == disabled)
        assert 'value="sha-' + "a" * 40 + '"' in html
    # Recent notifications sits in a collapsed fold with no remembered state.
    i = html.index("Recent notifications")
    assert '<section class="fold">' in html[html.rfind("<section", 0, i):i]
