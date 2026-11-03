"""Bound diagnostic repetition without changing test acceptance results."""

import json
import re


def compact_feedback(value, max_errors=12, max_chars=12000):
    """Keep distinct errors and assertion values; explicitly count omitted records."""
    def clean(text):
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        if len(text) > max_chars:
            return text[:max_chars] + f"\n[omitted {len(text) - max_chars} characters; full feedback is in compiler.log]"
        return text

    def visit(item):
        if isinstance(item, dict):
            if item.get("returncode") == 0:
                return {"returncode": 0}
            return {key: visit(child) for key, child in item.items()
                if not (key in {"output", "stdout", "stderr"} and item.get("failures"))}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, str):
            return clean(item)
        return item

    def report(item):
        if not isinstance(item, dict):
            return visit(item)
        result = {}
        for key, child in item.items():
            if key in {"failures", "errors"} and isinstance(child, list):
                unique, seen = [], set()
                for entry in child:
                    # Deduplicate before truncation; different assertion values stay distinct.
                    normalized = re.sub(r"\x1b\[[0-9;]*m", "", json.dumps(entry, ensure_ascii=False)
                        if not isinstance(entry, str) else entry)
                    if normalized not in seen:
                        seen.add(normalized)
                        unique.append(entry)
                result[key] = [visit(entry) for entry in unique[:max_errors]]
                if len(unique) > max_errors:
                    result[key + "_omitted"] = len(unique) - max_errors
                if len(unique) < len(child):
                    result[key + "_duplicates"] = len(child) - len(unique)
            elif key in {"output", "stdout", "stderr"} and item.get("failures"):
                continue
            else:
                result[key] = report(child) if isinstance(child, dict) else visit(child)
        return {"returncode": 0} if item.get("returncode") == 0 else result

    return report(value)
