"""Incremental entity and field updates."""

from copy import deepcopy


FIELD_TYPES = {"string", "integer", "number", "boolean", "date", "datetime", "uuid", "json"}


def validate_schema(schema):
    """Validate merged field declarations before SQL and obligation consumers use them."""
    entities = schema["entities"]
    for name, entity in entities.items():
        if "." in name:
            raise ValueError(f"Entity name {name!r} cannot contain the Entity.field separator '.'")
        if not isinstance(entity["description"], str):
            raise ValueError(f"Entity {name!r} description must be a string")
        if not entity["fields"]:
            raise ValueError(f"Entity {name!r} needs at least one field")
        field_names = set()
        for field_name, field in entity["fields"].items():
            if not field_name.strip() or "." in field_name or "\x00" in field_name:
                raise ValueError(f"Invalid field name {field_name!r} in {name!r}; use a nonempty name without '.' or NUL")
            if field_name.casefold() in field_names:
                raise ValueError(f"Field {name}.{field_name} collides with another field by case")
            field_names.add(field_name.casefold())
            properties = ("description", "type", "nullable", "primary_key", "unique", "references", "constraints")
            missing = [key for key in properties if key not in field]
            symbol = f"{name}.{field_name}"
            if missing:
                raise ValueError(f"Field {symbol} is missing: {', '.join(missing)}")
            kind = field["type"]
            if not isinstance(kind, str) or kind not in FIELD_TYPES:
                raise ValueError(f"Field {symbol} type must be one of {', '.join(sorted(FIELD_TYPES))}")
            if not isinstance(field["description"], str):
                raise ValueError(f"Field {symbol} description must be a string")
            for key in ("nullable", "primary_key", "unique"):
                if not isinstance(field[key], bool):
                    raise ValueError(f"Field {symbol} {key} must be a boolean")
            constraints = field["constraints"]
            if not isinstance(constraints, list) or any(not isinstance(item, str) for item in constraints):
                raise ValueError(f"Field {symbol} constraints must be an array of strings")
            reference = field["references"]
            if reference is not None:
                if not isinstance(reference, str):
                    raise ValueError(f"Field {symbol} references must be null or an existing Entity.field string")
                table, _, column = reference.partition(".")
                if column not in entities.get(table, {}).get("fields", {}):
                    raise ValueError(f"Field {symbol} references undefined field {reference!r}; "
                        "define it in entities or reference an existing field")


def check_related_fields(schema, fields):
    """Require database associations to resolve before obligation inference consumes them."""
    if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
        raise ValueError("related_fields must be an array of qualified Entity.field strings")
    defined = {f"{entity}.{field}" for entity, definition in schema["entities"].items()
        for field in definition["fields"]}
    missing = sorted(set(fields) - defined)
    if missing:
        raise ValueError(
            "related_fields references undefined database fields: " + ", ".join(missing)
            + ". Reuse exact existing names from symbol_inventory, or define the missing entities/fields "
            "in this response's entities update. related_fields does not create definitions; "
            "do not defer them to later requirements."
        )


def apply_patch(schema, patch):
    result = deepcopy(schema)
    entities = patch["entities"]
    if not isinstance(entities, dict):
        raise ValueError("entities must be an object keyed by entity name")
    for name, update in entities.items():
        if not isinstance(update, dict):
            raise ValueError(f"Entity {name!r} update must be an object")
        if name not in result["entities"] and "description" not in update:
            raise ValueError(f"New entity {name!r} requires a description")
        existing = result["entities"].setdefault(name, {"description": "", "fields": {}})
        if "description" in update:
            existing["description"] = update["description"]
        fields = update.get("fields", {})
        if not isinstance(fields, dict):
            raise ValueError(f"Entity {name!r} fields must be an object")
        for field, changes in fields.items():
            if not isinstance(changes, dict):
                raise ValueError(f"Field {name}.{field} update must be an object")
            existing["fields"].setdefault(field, {}).update(deepcopy(changes))
    return result
