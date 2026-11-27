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
    default: bool = True


KINDS: list[Kind] = [
    Kind("new_contract", "New courier contract", "A new contract is assigned to the corp.", "Hauling"),
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
    Kind("industry_job_ready", "Watched industry job ready",
         "A manufacturing job matching the rules below is ready for delivery.", "Industry"),
    Kind("spam_detected", "Possible contract spam",
         "An issuer was auto-muted for posting too many contracts.", "Safety & system"),
    Kind("token_invalid", "ESI login expired",
         "A character's ESI access was revoked and needs re-linking.", "Safety & system"),
    Kind("update_available", "New app version available", "", "Safety & system"),
]
KIND_BY_KEY = {k.key: k for k in KINDS}
ALWAYS_ON = {"test"}  # the System page's test DM

# Industry watch rules: matched against the built item's ESI group name.
# Only manufacturing jobs are considered, so reactions, research, copying and
# invention never alert.
INDUSTRY_RULES: dict[str, tuple[str, frozenset[str], bool]] = {
    "freighters": ("Freighters & jump freighters",
                   frozenset({"Freighter", "Jump Freighter"}), True),
    "capitals": ("Capital ships (carriers, dreads, FAX, capital industrials)",
                 frozenset({"Carrier", "Dreadnought", "Lancer Dreadnought", "Force Auxiliary",
                            "Capital Industrial Ship"}), True),
    "supercaps": ("Supercarriers & titans", frozenset({"Supercarrier", "Titan"}), True),
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


def industry_rules(db: Session) -> tuple[set[str], list[str]]:
    rules = {key for key, (_l, _g, default) in INDUSTRY_RULES.items()
             if _flag(db, f"industry:{key}", default)}
    raw = _load(db).get(PREFIX + "industry:keywords", "")
    keywords = [w.strip().lower() for w in raw.replace("\n", ",").split(",") if w.strip()]
    return rules, keywords


def job_rule_match(activity_id: int, product_name: str | None, group_name: str | None,
                   rules: set[str], keywords: list[str]) -> str | None:
    """Return the label of the first matching watch rule, or None."""
    if activity_id != 1:  # manufacturing only
        return None
    for key in rules:
        label, groups, _ = INDUSTRY_RULES[key]
        if group_name in groups:
            return label
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
    for k in KINDS:
        _set(db, k.key, "1" if form.get(f"kind_{k.key}") else "0")
    for key in INDUSTRY_RULES:
        _set(db, f"industry:{key}", "1" if form.get(f"rule_{key}") else "0")
    _set(db, "industry:keywords", (form.get("industry_keywords") or "").strip()[:1000])
    db.commit()
    invalidate()


def snapshot(db: Session) -> dict:
    """Everything the settings page needs."""
    rules, keywords = industry_rules(db)
    groups: dict[str, list[tuple[Kind, bool]]] = {}
    for k in KINDS:
        groups.setdefault(k.group, []).append((k, is_enabled(db, k.key)))
    return {"groups": groups,
            "rules": [(key, label, sorted(g), key in rules)
                      for key, (label, g, _d) in INDUSTRY_RULES.items()],
            "keywords": ", ".join(keywords)}
