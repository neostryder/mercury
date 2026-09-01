"""Persisted filtering policy for deterministic and semantic decisions.

The policy intentionally stays in one small JSON file. Authenticated sender
lists provide the deterministic fast path, semantic buckets are passed to the
judge, and a standing custom action can be associated with a sender address
or domain. Writes replace the file atomically so a process interruption
cannot leave a partially written policy behind.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 3
SENDER_LISTS = ("blacklist", "greylist", "whitelist")
SEMANTIC_DISPOSITIONS = ("550", "421", "250")
LIST_DISPOSITIONS = {"blacklist": "550", "greylist": "421", "whitelist": "250"}
LIST_VERDICTS = {"blacklist": "SPAM", "greylist": "UNSURE", "whitelist": "LEGIT"}

MIGRATED_LEWD_RULE = (
    "If the message proposes any lewd activities, relates to dating sites "
    "(real or fake), or offers lewd images"
)
MIGRATED_UNSOLICITED_SPAM_RULE = (
    "Hard bounce messages that are completely unsolicited and clearly spam"
)

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_ADDRESS_RE = re.compile(
    r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class PolicyConfigError(ValueError):
    """The persisted filtering policy is malformed or ambiguous."""


@dataclass(frozen=True)
class SenderListMatch:
    list_name: str
    selector: str
    disposition: str
    verdict: str
    matched_pattern: str | None = None


def sender_domain_is_authenticated(dmarc_result: object, claimed_domain: str | None) -> bool:
    """Whether ForwardEmail's own DMARC verdict authenticates the claimed sender.

    Uses the webhook payload's own `dmarc` field (ForwardEmail's mailauth-computed
    result, already evaluated against the message's actual From header) rather than
    re-parsing an Authentication-Results header out of the raw message. Those headers
    can carry a forged FOREIGN authserv-id - ForwardEmail only strips assertions
    forged as its own identity, not ones claiming to be some other server - so
    trusting them directly would reopen the exact spoofing gap this check exists to
    close. DMARC's own alignment check already answers "does SPF or DKIM align with
    the visible From domain", which is the same question this exists to answer, from
    a value Mercury already fully trusts (the same authenticated webhook payload
    every other field here comes from).

    Fails closed: missing, malformed, or non-"pass" data all return False. A domain
    with no published DMARC policy will never authenticate here - deliberate, since
    that is exactly the ambiguous case worth failing closed on before letting a
    message skip content screening entirely.
    """
    if not claimed_domain:
        return False
    if not isinstance(dmarc_result, dict):
        return False
    status = dmarc_result.get("status")
    if not isinstance(status, dict):
        return False
    result = status.get("result")
    return isinstance(result, str) and result.strip().lower() == "pass"


def empty_policy() -> dict:
    return {
        "version": SCHEMA_VERSION,
        "sender_lists": {name: [] for name in SENDER_LISTS},
        "blacklist_patterns": [],
        "semantic_rules": {disposition: [] for disposition in SEMANTIC_DISPOSITIONS},
        "custom_actions": [],
        "migration_warnings": [],
    }


def compile_blacklist_pattern(pattern: str) -> re.Pattern:
    """Compile a blacklist regex, raising ValueError on anything invalid.

    Patterns are matched full-string against a normalized domain only (never
    the full address or message body, and never re-run against attacker-sized
    input), so a pathological pattern's blast radius stays bounded by the same
    253-character cap _DOMAIN_RE already enforces on every domain selector.
    """
    normalized = (pattern or "").strip()
    if not normalized:
        raise ValueError("blacklist pattern must not be empty")
    try:
        return re.compile(normalized, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid blacklist pattern {normalized!r}: {exc}") from exc


def normalize_selector(selector: str) -> str:
    value = (selector or "").strip().lower().rstrip(".")
    if value.startswith("@"):
        value = value[1:]
    pattern = _ADDRESS_RE if "@" in value else _DOMAIN_RE
    if not pattern.fullmatch(value):
        raise ValueError("selector must be an exact email address or domain")
    return value


def selector_domain(selector: str) -> str:
    return selector.rsplit("@", 1)[-1]


def migrate_legacy_rules(rules: list[str]) -> dict:
    """Migrate the known flat ledger without inferring new policy.

    The production ledger being replaced has eight known entries. Recognition
    is deliberately tied to their known domains and phrases. Anything
    unexpected is retained as a warning for a human instead of being guessed
    into a disposition bucket where it could reject mail incorrectly.
    """
    policy = empty_policy()
    found_blacklist: set[str] = set()
    found_whitelist: set[str] = set()
    found_lewd = False
    found_unsolicited_spam = False

    for raw_rule in rules:
        rule = raw_rule.strip() if isinstance(raw_rule, str) else ""
        if not rule:
            continue
        lowered = rule.lower()
        recognized = False

        for domain in ("kickstarnow.com", "kickstartrack.com", "mail.beehiiv.com"):
            if domain in lowered:
                found_blacklist.add(domain)
                recognized = True

        if "immail.fanatical.com" in lowered:
            found_whitelist.add("immail.fanatical.com")
            recognized = True

        if "lewd" in lowered and ("dating" in lowered or "images" in lowered):
            found_lewd = True
            recognized = True

        if "completely unsolicited" in lowered and "clearly spam" in lowered:
            found_unsolicited_spam = True
            recognized = True

        if ("paypal" in lowered and "gog" in lowered) or "nellis auction" in lowered:
            recognized = True

        if not recognized:
            policy["migration_warnings"].append(rule)

    policy["sender_lists"]["blacklist"] = sorted(found_blacklist)
    policy["sender_lists"]["whitelist"] = sorted(found_whitelist)
    if found_lewd:
        policy["semantic_rules"]["550"].append(MIGRATED_LEWD_RULE)
    if found_unsolicited_spam:
        policy["semantic_rules"]["550"].append(MIGRATED_UNSOLICITED_SPAM_RULE)
    return policy


class FilteringPolicyStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return empty_policy()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise PolicyConfigError(f"could not read filtering policy: {exc}") from exc

        if isinstance(raw, dict) and isinstance(raw.get("rules"), list):
            migrated = migrate_legacy_rules(raw["rules"])
            self._save(migrated)
            return migrated

        if isinstance(raw, dict) and raw.get("version") == 2:
            upgraded = dict(raw)
            upgraded["version"] = SCHEMA_VERSION
            upgraded.setdefault("blacklist_patterns", [])
            validated = self._validate(upgraded)
            self._save(validated)
            return validated

        return self._validate(raw)

    def _validate(self, raw: object) -> dict:
        if not isinstance(raw, dict):
            raise PolicyConfigError("filtering policy must be a JSON object")
        if raw.get("version") != SCHEMA_VERSION:
            raise PolicyConfigError(f"unsupported filtering policy version: {raw.get('version')}")

        policy = empty_policy()
        sender_lists = raw.get("sender_lists")
        semantic_rules = raw.get("semantic_rules")
        custom_actions = raw.get("custom_actions")
        if not isinstance(sender_lists, dict):
            raise PolicyConfigError("sender_lists must be an object")
        if not isinstance(semantic_rules, dict):
            raise PolicyConfigError("semantic_rules must be an object")
        if not isinstance(custom_actions, list):
            raise PolicyConfigError("custom_actions must be a list")

        owners: dict[str, str] = {}
        for list_name in SENDER_LISTS:
            entries = sender_lists.get(list_name)
            if not isinstance(entries, list):
                raise PolicyConfigError(f"sender_lists.{list_name} must be a list")
            for entry in entries:
                if not isinstance(entry, str):
                    raise PolicyConfigError(f"sender_lists.{list_name} entries must be strings")
                try:
                    selector = normalize_selector(entry)
                except ValueError as exc:
                    raise PolicyConfigError(f"invalid sender selector {entry!r}: {exc}") from exc
                if selector in owners:
                    raise PolicyConfigError(
                        f"sender selector {selector!r} appears in both {owners[selector]} and {list_name}"
                    )
                owners[selector] = list_name
                policy["sender_lists"][list_name].append(selector)

        patterns = raw.get("blacklist_patterns", [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise PolicyConfigError("blacklist_patterns must be a list of strings")
        seen_patterns: set[str] = set()
        for pattern in (p.strip() for p in patterns if p.strip()):
            if pattern in seen_patterns:
                raise PolicyConfigError(f"blacklist pattern {pattern!r} appears more than once")
            try:
                compile_blacklist_pattern(pattern)
            except ValueError as exc:
                raise PolicyConfigError(str(exc)) from exc
            seen_patterns.add(pattern)
            policy["blacklist_patterns"].append(pattern)

        semantic_owners: dict[str, str] = {}
        for disposition in SEMANTIC_DISPOSITIONS:
            rules = semantic_rules.get(disposition)
            if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
                raise PolicyConfigError(f"semantic_rules.{disposition} must be a list of strings")
            for rule in (rule.strip() for rule in rules if rule.strip()):
                if rule in semantic_owners:
                    raise PolicyConfigError(
                        f"semantic rule appears in both {semantic_owners[rule]} and {disposition}"
                    )
                semantic_owners[rule] = disposition
                policy["semantic_rules"][disposition].append(rule)

        action_selectors: set[str] = set()
        for entry in custom_actions:
            if not isinstance(entry, dict):
                raise PolicyConfigError("custom action entries must be objects")
            try:
                selector = normalize_selector(entry.get("selector", ""))
            except ValueError as exc:
                raise PolicyConfigError(f"invalid custom action selector: {exc}") from exc
            instruction = entry.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                raise PolicyConfigError("custom action instruction must be a non-empty string")
            if selector in action_selectors:
                raise PolicyConfigError(f"custom action selector {selector!r} appears more than once")
            action_selectors.add(selector)
            normalized = {"selector": selector, "instruction": instruction.strip()}
            native = entry.get("native")
            if native is not None:
                if (
                    not isinstance(native, dict)
                    or native.get("kind") != "folder"
                    or not isinstance(native.get("folder"), str)
                    or not native["folder"].strip()
                ):
                    raise PolicyConfigError("native custom action must be a non-empty folder action")
                normalized["native"] = {"kind": "folder", "folder": native["folder"].strip()}
            policy["custom_actions"].append(normalized)

        warnings = raw.get("migration_warnings", [])
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            raise PolicyConfigError("migration_warnings must be a list of strings")
        policy["migration_warnings"] = list(warnings)
        return policy

    def _save(self, policy: dict) -> None:
        validated = self._validate(policy)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(validated, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def snapshot(self) -> dict:
        return copy.deepcopy(self.load())

    def match_sender(
        self, address: str | None, policy: dict | None = None
    ) -> SenderListMatch | None:
        if not address:
            return None
        try:
            normalized_address = normalize_selector(address)
        except ValueError:
            return None
        if "@" not in normalized_address:
            return None
        domain = selector_domain(normalized_address)
        policy = policy or self.load()
        for selector in (normalized_address, domain):
            for list_name in SENDER_LISTS:
                if selector in policy["sender_lists"][list_name]:
                    return SenderListMatch(
                        list_name=list_name,
                        selector=selector,
                        disposition=LIST_DISPOSITIONS[list_name],
                        verdict=LIST_VERDICTS[list_name],
                    )
        for pattern in policy["blacklist_patterns"]:
            try:
                compiled = compile_blacklist_pattern(pattern)
            except ValueError:
                continue
            if compiled.fullmatch(domain):
                return SenderListMatch(
                    list_name="blacklist",
                    selector=domain,
                    disposition=LIST_DISPOSITIONS["blacklist"],
                    verdict=LIST_VERDICTS["blacklist"],
                    matched_pattern=pattern,
                )
        return None

    def put_sender(self, list_name: str, selector: str) -> dict:
        if list_name not in SENDER_LISTS:
            raise ValueError(f"unknown sender list: {list_name}")
        normalized = normalize_selector(selector)
        policy = self.load()
        removed_from = []
        for candidate in SENDER_LISTS:
            if normalized in policy["sender_lists"][candidate]:
                policy["sender_lists"][candidate].remove(normalized)
                if candidate != list_name:
                    removed_from.append(candidate)
        if normalized not in policy["sender_lists"][list_name]:
            policy["sender_lists"][list_name].append(normalized)
            policy["sender_lists"][list_name].sort()
        self._save(policy)
        return {"selector": normalized, "list": list_name, "removed_from": removed_from}

    def remove_sender(self, list_name: str, selector: str) -> bool:
        if list_name not in SENDER_LISTS:
            raise ValueError(f"unknown sender list: {list_name}")
        normalized = normalize_selector(selector)
        policy = self.load()
        if normalized not in policy["sender_lists"][list_name]:
            return False
        policy["sender_lists"][list_name].remove(normalized)
        self._save(policy)
        return True

    def add_blacklist_pattern(self, pattern: str) -> bool:
        normalized = compile_blacklist_pattern(pattern).pattern
        policy = self.load()
        if normalized in policy["blacklist_patterns"]:
            return False
        policy["blacklist_patterns"].append(normalized)
        policy["blacklist_patterns"].sort()
        self._save(policy)
        return True

    def remove_blacklist_pattern(self, pattern: str) -> bool:
        normalized = (pattern or "").strip()
        policy = self.load()
        if normalized not in policy["blacklist_patterns"]:
            return False
        policy["blacklist_patterns"].remove(normalized)
        self._save(policy)
        return True

    def add_semantic_rule(self, disposition: str, rule: str) -> bool:
        if disposition not in SEMANTIC_DISPOSITIONS:
            raise ValueError(f"unknown semantic disposition: {disposition}")
        normalized = (rule or "").strip()
        if not normalized:
            raise ValueError("semantic rule must not be empty")
        policy = self.load()
        if normalized in policy["semantic_rules"][disposition]:
            return False
        for candidate in SEMANTIC_DISPOSITIONS:
            if normalized in policy["semantic_rules"][candidate]:
                policy["semantic_rules"][candidate].remove(normalized)
        policy["semantic_rules"][disposition].append(normalized)
        self._save(policy)
        return True

    def remove_semantic_rule(self, disposition: str, rule: str) -> bool:
        if disposition not in SEMANTIC_DISPOSITIONS:
            raise ValueError(f"unknown semantic disposition: {disposition}")
        policy = self.load()
        if rule not in policy["semantic_rules"][disposition]:
            return False
        policy["semantic_rules"][disposition].remove(rule)
        self._save(policy)
        return True

    def put_custom_action(
        self, selector: str, instruction: str, native_folder: str | None = None
    ) -> dict:
        normalized_selector = normalize_selector(selector)
        normalized_instruction = (instruction or "").strip()
        if not normalized_instruction:
            raise ValueError("custom action instruction must not be empty")
        folder = (native_folder or "").strip() or None
        policy = self.load()
        entry = {"selector": normalized_selector, "instruction": normalized_instruction}
        if folder:
            entry["native"] = {"kind": "folder", "folder": folder}
        policy["custom_actions"] = [
            existing
            for existing in policy["custom_actions"]
            if existing["selector"] != normalized_selector
        ]
        policy["custom_actions"].append(entry)
        policy["custom_actions"].sort(key=lambda item: item["selector"])
        self._save(policy)
        return entry

    def remove_custom_action(self, selector: str) -> bool:
        normalized = normalize_selector(selector)
        policy = self.load()
        remaining = [
            entry for entry in policy["custom_actions"] if entry["selector"] != normalized
        ]
        if len(remaining) == len(policy["custom_actions"]):
            return False
        policy["custom_actions"] = remaining
        self._save(policy)
        return True

    def match_custom_action(
        self, address: str | None, policy: dict | None = None
    ) -> dict | None:
        if not address:
            return None
        try:
            normalized_address = normalize_selector(address)
        except ValueError:
            return None
        if "@" not in normalized_address:
            return None
        domain = selector_domain(normalized_address)
        policy = policy or self.load()
        by_selector = {entry["selector"]: entry for entry in policy["custom_actions"]}
        entry = by_selector.get(normalized_address) or by_selector.get(domain)
        return copy.deepcopy(entry) if entry else None
