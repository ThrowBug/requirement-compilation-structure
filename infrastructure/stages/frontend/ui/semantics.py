"""Admit only explicit, unambiguous presentation contracts grounded in the input."""

import re


ROLES = frozenset("""alert button checkbox combobox dialog form heading img link list
listbox listitem menu menuitem navigation option password radio radiogroup region
searchbox slider spinbutton status switch tab table textbox toolbar tree treeitem""".split())
SENTENCE = re.compile(r'(?<=[。！？!?])|(?<=\.)\s+|\n')
ROLE_REFERENCE = re.compile(r'["“]([^"”\r\n]+)["”]\s*\(\s*role\s*=\s*([a-z][\w-]*)\s*\)')
QUOTED = re.compile(r'["“][^"”\r\n]+["”]')
# Unsupported scoped assertions belong in browser scenarios. This is a conservative
# filter on model-proposed evidence, not a parser for arbitrary requirement prose.
SCOPED = re.compile(r"\b(?:no|not|never|without|cannot|if|unless|when|only|either|or|may|except|until|after|before)\b"
    r"|n['’]t\b|不得|不应|禁止|不可|不能|不显示|不出现|无需|若|如果|仅当|时才|或者|或是|可选", re.I)


def validated_contracts(design, tree):
    """Return verified checks by element ID, and candidates requiring scenario review."""
    checks, conflicts, deferred = {}, set(), []
    if not isinstance(design.get("presentation_contracts", {}), dict):
        return {}, [{"reason": "invalid presentation contract collection"}]
    for rid, entries in design.get("presentation_contracts", {}).items():
        if rid not in tree.requirements or not isinstance(entries, list):
            deferred.append({"requirement_id": rid, "reason": "invalid presentation contract owner or list"})
            continue
        texts = [tree.requirements[rid]["description"],
            *(tree.steps[sid]["Then"] for sid in tree.own_steps[rid])]
        sentences = {sentence.strip() for value in texts if isinstance(value, str)
            for sentence in SENTENCE.split(value) if sentence.strip()}
        owned = set(design["requirements"].get(rid, ()))
        for entry in entries:
            record = {"requirement_id": rid, "reason": "ambiguous or unverified presentation contract"}
            if not isinstance(entry, dict):
                deferred.append(record)
                continue
            uid, role, name, evidence = (entry.get(key) for key in ("element_id", "role", "name", "evidence"))
            if not all(isinstance(item, str) and item.strip() for item in (uid, role, name, evidence)):
                deferred.append(record)
                continue
            record["element_id"] = uid
            role_evidence = (r'\bpassword\s+(?:inputs?|fields?)\b|密码(?:字段|输入框)'
                if role == "password" else rf'\b{re.escape(role)}\b')
            pairs = ROLE_REFERENCE.findall(evidence)
            clause = QUOTED.sub("", evidence)
            if (uid not in owned or uid not in design["elements"] or role not in ROLES
                    or evidence.strip() not in sentences or SCOPED.search(clause)
                    or not re.search(role_evidence, clause, re.I)
                    or (pairs and role != "password" and (name, role) not in pairs)
                    or not re.search(rf'(?<!\w){re.escape(name)}(?!\w)', evidence)):
                deferred.append(record)
                continue
            if uid in checks and (checks[uid]["role"], checks[uid]["name"]) != (role, name):
                conflicts.add(uid)
                deferred.append({**record, "reason": "conflicting contracts for one element"})
                continue
            checks.setdefault(uid, {"role": role, "name": name, "requirement_id": rid, "evidence": evidence})
    return {uid: check for uid, check in checks.items() if uid not in conflicts}, deferred
