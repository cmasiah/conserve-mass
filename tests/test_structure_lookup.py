"""A 403 on a structure means *that* character has no access: try the items' owner,
then the others, and say who was tried."""
import asyncio
import logging

from tests.test_blueprints_esi import _add_char

SCOPES = "esi-universe.read_structures.v1"
SID = 1_046_043_874_325


def _fake_get(allowed: set[str], seen: list[str]):
    from app.esi import client as esi

    async def get(path, token=None, **kw):
        seen.append(token)
        if token in allowed:
            return {"name": "AC2E-3 - Home Fortizar", "solar_system_id": 30000001}
        raise esi.EsiError(403, '{"error":"Forbidden"}')
    return get


def test_owner_is_tried_first_and_others_after_a_403(db, monkeypatch):
    from app.esi import client as esi
    from app.esi import resolver
    _add_char(db, 1, "First Char", SCOPES)
    _add_char(db, 2, "Item Owner", SCOPES)
    db.commit()
    seen: list[str] = []
    monkeypatch.setattr(esi, "get", _fake_get({"tok2"}, seen))
    monkeypatch.setattr(resolver, "_system", lambda sid: asyncio.sleep(0, ("AC2E-3", -0.4)))
    loc = asyncio.run(resolver.resolve_location(db, SID, None, prefer=[2]))
    assert loc.name == "AC2E-3 - Home Fortizar" and seen == ["tok2"]

    # The sync's own token is denied: the others still get a turn.
    db.delete(loc); db.commit()
    seen.clear()
    loc = asyncio.run(resolver.resolve_location(db, SID, "tokX"))
    assert loc.name and seen[0] == "tokX" and "tok2" in seen


def test_log_names_who_was_denied_and_where_it_came_from(db, monkeypatch, caplog):
    from app.esi import client as esi
    from app.esi import resolver
    _add_char(db, 1, "First Char", SCOPES)
    _add_char(db, 2, "Item Owner", SCOPES)
    db.commit()
    monkeypatch.setattr(esi, "get", _fake_get(set(), []))
    with caplog.at_level(logging.INFO, logger="app.esi.resolver"):
        loc = asyncio.run(resolver.resolve_location(db, SID, None, prefer=[2],
                                                    source="assets of Item Owner"))
    assert loc.name is None
    msg = caplog.text
    assert str(SID) in msg and "assets of Item Owner" in msg
    assert "Item Owner, First Char" in msg          # owner tried first
