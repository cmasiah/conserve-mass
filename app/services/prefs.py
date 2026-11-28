"""Notification preferences: which Discord DMs you want, and which industry jobs to watch.

Stored as rows in the `settings` table (keys starting with "notify:"), cached in memory
and refreshed whenever the settings page saves.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

PREFIX = "notify:"


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    help: str
    group: str
    default: bool = True   # only for kinds that existed before; new kinds start off


KINDS: list[Kind] = [
    Kind("new_contract", "New courier received", "A new contract is assigned to the corp.", "Hauling"),
    Kind("expiring", "Unaccepted contract expiring soon",
         "An outstanding contract expires within EXPIRY_WARNING_HOURS.", "Hauling"),
    Kind("delivery_due", "Delivery deadline approaching",
         "An accepted contract must be delivered within EXPIRY_WARNING_HOURS.", "Hauling"),
    Kind("status_accepted", "Contract accepted", "Someone accepted a contract.", "Hauling"),
    Kind("status_finished", "Contract delivered", "A contract was completed.", "Hauling"),
    Kind("status_failed", "Contract failed", "A contract failed.", "Hauling"),
    Kind("status_cancelled", "Contract cancelled by issuer", "", "Hauling"),
    Kind("status_deleted", "Contract deleted by issuer", "", "Hauling"),
    Kind("status_rejected", "Contract rejected", "", "Hauling"),
    Kind("industry_job_ready", "Watched ship or item built",
         "A manufacturing job matching the groups or keywords below is ready for delivery.",
         "Industry"),
    Kind("research_job_ready", "Research job finished",
         "ME or TE research, copying or invention finished and is ready to deliver.",
         "Industry", default=False),
    Kind("spam_detected", "Possible contract spam",
         "An issuer was auto-muted for posting too many contracts.", "Safety & system"),
    Kind("token_invalid", "ESI login expired",
         "A character's ESI access was revoked and needs re-linking.", "Safety & system"),
    Kind("update_available", "New main release",
         "Once per new version published to main (stable). Beta builds never ping.",
         "Safety & system"),
]


@dataclass(frozen=True)
class Switch:
    """One toggle on the settings page. Most control one kind; a few cover several
    closely related contract updates, so the page stays short."""
    key: str
    label: str
    help: str
    group: str
    covers: tuple[str, ...]


_COMBINED = {
    "status_progress": ("Contract accepted, delivered, or failed",
                        "Someone accepted, completed or failed one of the corp's contracts.",
                        ("status_accepted", "status_finished", "status_failed")),
    "status_closed": ("Contract cancelled, deleted, or rejected",
                      "The issuer cancelled or deleted it, or it was rejected.",
                      ("status_cancelled", "status_deleted", "status_rejected")),
}
_IN_COMBINED = {k: sw for sw, (_l, _h, ks) in _COMBINED.items() for k in ks}


def _switches() -> list[Switch]:
    out, done = [], set()
    for k in KINDS:
        sw = _IN_COMBINED.get(k.key)
        if sw is None:
            out.append(Switch(k.key, k.label, k.help, k.group, (k.key,)))
        elif sw not in done:
            done.add(sw)
            label, help_, covers = _COMBINED[sw]
            out.append(Switch(sw, label, help_, k.group, covers))
    return out


SWITCHES = _switches()
KIND_BY_KEY = {k.key: k for k in KINDS}
ALWAYS_ON = {"test"}  # the System page's test DM

# Industry watch rules: matched against the built item's ESI group name.
# Only manufacturing jobs are considered (research has its own alert above).
CAPITAL_GROUPS = frozenset({"Carrier", "Dreadnought", "Lancer Dreadnought", "Force Auxiliary",
                            "Capital Industrial Ship", "Supercarrier", "Titan", "Freighter",
                            "Jump Freighter"})


@dataclass(frozen=True)
class Rule:
    label: str
    groups: frozenset[str]
    legacy: str          # older setting this inherits from until the page is saved


INDUSTRY_RULES: dict[str, Rule] = {
    "freighters": Rule("Freighters", frozenset({"Freighter"}), "freighters"),
    "jump_freighters": Rule("Jump Freighters", frozenset({"Jump Freighter"}), "freighters"),
    "rorquals": Rule("Rorquals", frozenset({"Capital Industrial Ship"}), "capitals"),
    "capitals": Rule("All capital ships", CAPITAL_GROUPS, "capitals"),
    "supers": Rule("Supercarriers", frozenset({"Supercarrier"}), "supercaps"),
    "titans": Rule("Titans", frozenset({"Titan"}), "supercaps"),
}
RULE_HELP = {
    "capitals": "Carriers, dreadnoughts, FAX, Rorquals, freighters, JFs, supers and titans",
}

_cache: dict[str, str] | None = None


def _load(db: Session) -> dict[str, str]:
    global _cache
    if _cache is None:
        _cache = {s.key: s.value for s in
                  db.scalars(select(Setting).where(Setting.key.like(f"{PREFIX}%")))}
    return _cache


def invalidate() -> None:
    global _cache
    _cache = None


def _flag(db: Session, key: str, default: bool) -> bool:
    v = _load(db).get(PREFIX + key)
    return default if v is None else v == "1"


def is_enabled(db: Session, kind: str) -> bool:
    if kind in ALWAYS_ON:
        return True
    k = KIND_BY_KEY.get(kind)
    return _flag(db, kind, k.default if k else True)


def _rule_on(db: Session, key: str) -> bool:
    """A rule's saved value; before the page is saved with it, whatever the older,
    coarser setting said (those defaulted on), so upgrading changes nothing."""
    saved = _load(db).get(f"{PREFIX}industry:{key}")
    if saved is not None:
        return saved == "1"
    legacy = _load(db).get(f"{PREFIX}industry:{INDUSTRY_RULES[key].legacy}")
    return True if legacy is None else legacy == "1"


def industry_rules(db: Session) -> tuple[set[str], list[str]]:
    rules = {key for key in INDUSTRY_RULES if _rule_on(db, key)}
    raw = _load(db).get(PREFIX + "industry:keywords", "")
    keywords = [w.strip().lower() for w in raw.replace("\n", ",").split(",") if w.strip()]
    return rules, keywords


def job_rule_match(activity_id: int, product_name: str | None, group_name: str | None,
                   rules: set[str], keywords: list[str]) -> str | None:
    """Return the label of the first matching watch rule, or None."""
    if activity_id != 1:  # manufacturing only
        return None
    # Most specific rule first, so a titan says "Titans", not "All capital ships".
    for key in sorted(rules, key=lambda k: len(INDUSTRY_RULES[k].groups)):
        rule = INDUSTRY_RULES[key]
        if group_name in rule.groups:
            return rule.label
    name = (product_name or "").lower()
    for kw in keywords:
        if kw in name:
            return f'keyword "{kw}"'
    return None


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, PREFIX + key) or Setting(key=PREFIX + key, value=value)
    row.value = value
    db.add(row)


def save(db: Session, form: dict[str, str]) -> None:
    """form: submitted checkbox names (present = on) plus 'industry_keywords'."""
    for sw in SWITCHES:
        on = "1" if form.get(f"kind_{sw.key}") else "0"
        for kind in sw.covers:
            _set(db, kind, on)
    for key in INDUSTRY_RULES:
        _set(db, f"industry:{key}", "1" if form.get(f"rule_{key}") else "0")
    _set(db, "industry:keywords", (form.get("industry_keywords") or "").strip()[:1000])
    db.commit()
    invalidate()


def snapshot(db: Session) -> dict:
    """Everything the settings page needs."""
    rules, keywords = industry_rules(db)
    groups: dict[str, list[tuple[Switch, bool]]] = {}
    for sw in SWITCHES:
        # A combined switch shows on if any part is on (older settings may differ);
        # saving the page sets all its parts the same.
        groups.setdefault(sw.group, []).append(
            (sw, any(is_enabled(db, k) for k in sw.covers)))
    return {"groups": groups,
            "rules": [(key, r.label, RULE_HELP.get(key) or ", ".join(sorted(r.groups)),
                       key in rules) for key, r in INDUSTRY_RULES.items()],
            "keywords": keywords}
