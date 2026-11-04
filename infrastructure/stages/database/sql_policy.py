"""Catch clock-dependent CHECK expressions before SQLite reaches a write."""

import re

from infrastructure.utils.files import read_source


_TOKEN = re.compile(r"--[^\n]*|/\*.*?(?:\*/|\Z)|'(?:''|[^'])*'|"
    r'"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|[a-zA-Z_][a-zA-Z_0-9$]*|[^\s]', re.S)
_DATE_FUNCTIONS = {"date", "time", "datetime", "julianday", "unixepoch", "strftime", "timediff"}


def _arguments(tokens, opening):
    """Return tokens inside one balanced parenthesized expression."""
    depth = 1
    for end in range(opening + 1, len(tokens)):
        depth += (tokens[end] == "(") - (tokens[end] == ")")
        if not depth:
            return tokens[opening + 1:end]
    return []  # Invalid SQL is still diagnosed by the real SQLite gate.


def _parameters(tokens):
    if not tokens:
        return []
    parameters = [[]]
    depth = 0
    for token in tokens:
        if token == "," and not depth:
            parameters.append([])
        else:
            parameters[-1].append(token)
            depth += (token == "(") - (token == ")")
    return parameters


def _literal(tokens):
    while tokens[:1] == ["("] and tokens[1:-1] == _arguments(tokens, 0):
        tokens = tokens[1:-1]
    return tokens[0] if len(tokens) == 1 and tokens[0].startswith("'") else None


def check_table_sql(sql, path):
    tokens = [token.lower() for token in _TOKEN.findall(sql) if not token.startswith(("--", "/*"))]
    for index, token in enumerate(tokens[:-1]):
        if token != "check" or tokens[index + 1] != "(":
            continue
        expression = _arguments(tokens, index + 1)
        for offset, name in enumerate(expression):
            dependent = name in {"current_date", "current_time", "current_timestamp"}
            function = name.strip('"`[]')
            if function in _DATE_FUNCTIONS and expression[offset + 1:offset + 2] == ["("]:
                args = _parameters(_arguments(expression, offset + 1))
                dates = args[1:] if function == "strftime" else args
                if function == "timediff":
                    dependent = any(_literal(arg) in {"'now'", "'subsec'", "'subsecond'"} for arg in dates)
                else:
                    dependent = (not dates or _literal(dates[0]) in {"'now'", "'subsec'", "'subsecond'"}
                        or any(_literal(arg) in {"'localtime'", "'utc'"} for arg in dates[1:]))
            if dependent:
                raise ValueError(f"{path}: SQLite CHECK uses clock-dependent {name}. "
                    "Keep deterministic format/range constraints in SQL and enforce comparisons with the current "
                    "date in the application before writing; do not remove the requirement or pre-create a replacement table.")


def check_existing_tables(output, schema):
    for name in schema["entities"]:
        relative = f"backend/src/db/tables/{name}.sql"
        path = output / relative
        if path.is_file():
            check_table_sql(read_source(path), relative)
