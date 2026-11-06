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

DIRECT_ROLES = {role: role for role in ROLES - {"password"}}
DIRECT_ROLES["image"] = "img"
INPUT_ROLES = {"password": "password", "number": "spinbutton", "search": "searchbox",
    "checkbox": "checkbox", "radio": "radio", "button": "button", "submit": "button",
    "text": "textbox", "email": "textbox", "tel": "textbox", "url": "textbox"}


def control_identity(node):
    """Find a static role/name in the UI design; placeholders are not names."""
    kind, props = node.get("type"), node.get("prop", {})
    if not isinstance(props, dict) or not isinstance(kind, str):
        return None
    role = props.get("role")
    if role is None:
        if kind == "input":
            input_type = props.get("input_type", "text")
            role = INPUT_ROLES.get(input_type) if isinstance(input_type, str) else None
        elif kind == "select":
            size = props.get("size")
            role = "listbox" if props.get("multiple") is True or type(size) is int and size > 1 else "combobox"
        elif kind == "textarea":
            role = "textbox"
        else:
            role = DIRECT_ROLES.get(kind)
    if not isinstance(role, str) or role not in ROLES:
        return None
    names = {props[key] for key in ("accessible_name", "aria_label", "aria-label", "label", "text",
        *(("alt",) if role == "img" else ())) if isinstance(props.get(key), str) and props[key].strip()}
    return (role, next(iter(names))) if len(names) == 1 else None


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
        identities = {}
        for uid in sorted(owned & design["elements"].keys()):
            if identity := control_identity(design["elements"][uid]):
                identities.setdefault(identity, []).append(uid)
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
            if (role not in ROLES
                    or evidence.strip() not in sentences or SCOPED.search(clause)
                    or not re.search(role_evidence, clause, re.I)
                    or (pairs and role != "password" and (name, role) not in pairs)
                    or not re.search(rf'(?<!\w){re.escape(name)}(?!\w)', evidence)):
                deferred.append(record)
                continue
            candidates = identities.get((role, name), [])
            if len(candidates) != 1:
                deferred.append({**record, "reason": "role and name do not identify one owned element",
                    "candidate_element_ids": candidates})
                continue
            original_uid, uid = uid, candidates[0]
            if uid in checks and (checks[uid]["role"], checks[uid]["name"]) != (role, name):
                conflicts.add(uid)
                deferred.append({**record, "reason": "conflicting contracts for one element"})
                continue
            check = {"role": role, "name": name, "requirement_id": rid, "evidence": evidence}
            if original_uid != uid:
                check["original_element_id"] = original_uid
            checks.setdefault(uid, check)
    return {uid: check for uid, check in checks.items() if uid not in conflicts}, deferred
