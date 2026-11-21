"""Index definitions describing the keys a model's items are stored and found under.

Each key is a `KeyPair`: an attribute name, and either a `str.format` template its value
is generated from, e.g. `"USER#{user_id}"`, or no template, when the key is the model
field of the same name.
"""

import string
from collections.abc import Mapping, Sequence
from typing import Any

import pydantic

_formatter = string.Formatter()


def _unparse_format_string(parsed_tuples) -> str:

    parts = []
    for literal_text, field_name, format_spec, conversion in parsed_tuples:
        # 1. Re-escape any literal braces in the plain text
        escaped_literal = literal_text.replace("{", "{{").replace("}", "}}")
        parts.append(escaped_literal)

        # 2. Rebuild the replacement field if present
        if field_name is not None:
            field = f"{{{field_name}"
            if conversion:
                field += f"!{conversion}"
            if format_spec:
                field += f":{format_spec}"
            field += "}"
            parts.append(field)

    return "".join(parts)


class KeyPair:
    """A key attribute name and the template its value is generated from, if any.

    Attributes:
        name: The key attribute name, e.g. `"PK"`
        template: The template the value is generated from, or None if the key is the
            model field named `name`
        is_column: Whether the key is the model field named `name`, rather than
            generated from a template
        attributes: The model attributes the key's value is built from, in template
            order; a column key is built from its own field
    """

    def __init__(self, name: str, template: str | None = None):
        """Record the key and the attribute paths its template references.

        Raises:
            ValueError: If a template field isn't an attribute name or path, such as
                `{}`, `{0}` or `{a[0]}`
        """
        self.name = name
        self.template = template or None
        self.is_column = self.template is None
        self._fields = []
        self.attributes: tuple[str, ...] = (name,)

        if self.template is not None:
            self._fields = list(_formatter.parse(self.template))

            # Literal-only segments of the template have no attribute
            self.attributes = tuple(
                attribute
                for _, attribute, _, _ in self._fields
                if attribute is not None
            )

    def __str__(self) -> str:
        """Describe the key, e.g. `<Key: PK (template 'USER#{user_id}')>` or `<Key: id (column)>`."""
        if self.is_column:
            return f"<Key: {self.name} (column)>"
        return f"<Key: {self.name} (template {self.template!r})>"

    def validate(self, model: type[pydantic.BaseModel]):
        """Check the key only references what the model defines.

        A column key must be a field or computed field stored under its own name, as the
        model dump writes it. A template key's attribute paths must each exist on the
        model.

        Raises:
            TypeError: If a path goes through an attribute that isn't a nested model
            ValueError: If a column key isn't a dumped field, is stored under a
                different alias, or a path's last attribute isn't defined
        """

        # A column key is written by the model dump, so must be a dumped field
        if self.is_column:
            field = model.model_fields.get(self.name)
            computed = model.model_computed_fields.get(self.name)
            if field is not None:
                alias = field.serialization_alias or field.alias
            elif computed is not None:
                alias = computed.alias
            else:
                raise ValueError(
                    f"Key {self.name!r} is a column, but {model.__name__} has no field "
                    f"or computed field named {self.name!r}"
                )

            # The dump stores the field under its alias, where the key wouldn't find it
            if alias is None or alias == self.name:
                return
            raise ValueError(
                f"Key {self.name!r} is a column, but {model.__name__} stores it as "
                f"{alias!r}: a column key must be stored under its own name"
            )

        # Check the template to ensure that all fields are found
        missing_attributes = []
        for _, attribute, _, _ in self._fields:
            if attribute is not None and not (
                model.model_fields.get(attribute)
                or model.model_computed_fields.get(attribute)
                or hasattr(model, attribute)
            ):
                missing_attributes.append(attribute)

        if missing_attributes:
            raise ValueError(
                f"{model.__name__} is missing required attributes/properties: {missing_attributes}"
            )

    def extract_fields(self, values: Mapping[str, Any], required: bool = True):
        """Split `values` into the key's attribute values, in template order, and the rest.

        Args:
            values: Attribute values by name
            required: Raise if an attribute has no value, rather than stopping there

        Raises:
            ValueError: If `required` and an attribute the key needs has no value
        """
        key_fields, extra = {}, dict(values)

        for attribute in self.attributes:
            if attribute in extra:
                key_fields[attribute] = extra.pop(attribute)
            elif required:
                raise ValueError(f"missing attribute {attribute}")
            else:
                break

        return key_fields, extra

    def format(self, values: Mapping[str, Any], partial: bool = False) -> str:
        """Get the key's value: the template filled from `values`, or the column's value.

        Raises:
            KeyError: If a value the key needs isn't in `values`
        """

        if self.template:
            parts = []
            for field in self._fields:
                if field[1] is None or field[1] in values:
                    parts.append(field)
                    continue
                elif partial:
                    parts.append((field[0], None, None, None))
                    break
                else:
                    raise ValueError("Not all required attributes were provided")
            return _unparse_format_string(parts).format_map(values)
        return values[self.name]


class Index:
    """The partition and sort key shared by the primary and global secondary indexes.

    Attributes:
        partition: The partition key
        sort: The sort key
        attributes: The model attributes either key is built from
    """

    def __init__(self, partition: KeyPair, sort: KeyPair):
        """Record the index's partition and sort keys."""
        self.partition = partition
        self.sort = sort
        self.attributes = frozenset({*partition.attributes, *sort.attributes})

    def validate(self, model: type[pydantic.BaseModel]):
        """Check both keys only reference attributes the model defines.

        Raises:
            TypeError: If a key's path goes through an attribute that isn't a model
            ValueError: If a key references an attribute the model doesn't define
        """
        self.partition.validate(model)
        self.sort.validate(model)

    def format(self, values: Mapping[str, Any], partial: bool = False):
        """Get the index's partition and sort key values from attribute values.

        Raises:
            ValueError: If `values` holds an attribute neither key is built from, or a
                value a template key needs isn't in `values`
            KeyError: If a column key's value isn't in `values`
        """
        # A value neither key uses is a mistake, which would otherwise be ignored
        unknown = sorted(values.keys() - self.attributes)
        if unknown:
            raise ValueError(
                f"The key is not built from the attribute(s): {', '.join(unknown)}"
            )

        return {
            self.partition.name: self.partition.format(values, partial=False),
            self.sort.name: self.sort.format(values, partial=partial),
        }

    def stored_key(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Get the index's partition and sort key values from an item as stored."""
        return {
            self.partition.name: item[self.partition.name],
            self.sort.name: item[self.sort.name],
        }


class PrimaryIndex(Index):
    """A primary index is a unique identifier for a model instance.

    It is used to identify the instance in the database, and to update or delete it.
    """


class LocalIndex:
    """An alternative sort key, which shares the primary index's partition key.

    Attributes:
        name: The index name, e.g. `"LSI1"`
        sort: The sort key
    """

    def __init__(self, name: str, sort: KeyPair):
        """Record the index's name and sort key."""
        self.name = name
        self.sort = sort

    def validate(self, model: type[pydantic.BaseModel]):
        """Check the sort key only references attributes the model defines.

        Raises:
            TypeError: If the key's path goes through an attribute that isn't a model
            ValueError: If the key references an attribute the model doesn't define
        """
        self.sort.validate(model)

    def generate(self, values: dict[str, Any], partial: bool = False):
        """Get the sort key value from attribute values.

        The partition key is the primary index's, so it isn't included.

        Raises:
            KeyError: If a value the key needs isn't in `values`
        """
        return {
            self.sort.name: self.sort.format(values, partial=partial),
        }


class GlobalSecondaryIndex(Index):
    """An alternative partition and sort key, under its own name.

    Attributes:
        name: The index name, e.g. `"GSI1"`
    """

    def __init__(self, name: str, partition: KeyPair, sort: KeyPair):
        """Record the index's name and its partition and sort keys."""
        super().__init__(partition, sort)
        self.name = name


def single_table_indexes(
    base: tuple[str | None, str | None],
    local: Sequence[str | None] = (),
    global_indexes: Sequence[tuple[str | None, str | None]] = (),
) -> list[PrimaryIndex | LocalIndex | GlobalSecondaryIndex]:
    """Create a model's indexes using single-table naming conventions.

    Keys are named `PK`/`SK`, `LSI<n>SK` and `GSI<n>PK`/`GSI<n>SK`, numbered in the
    order the indexes are given. A None template makes that key the model field of the
    same name, e.g. a field named `PK`.

    Args:
        base: The primary index's partition and sort key templates
        local: Each local secondary index's sort key template
        global_indexes: Each global secondary index's partition and sort key templates

    Returns:
        The primary index followed by the local and then the global indexes
    """
    pk_template, sk_template = base

    # The primary index always comes first
    indexes: list[PrimaryIndex | LocalIndex | GlobalSecondaryIndex] = [
        PrimaryIndex(KeyPair("PK", pk_template), KeyPair("SK", sk_template))
    ]

    # Local indexes only define a sort key
    for number, lsi_sk_template in enumerate(local, start=1):
        sort = KeyPair(f"LSI{number}SK", lsi_sk_template)
        indexes.append(LocalIndex(f"LSI{number}", sort))

    # Global indexes define both a partition and sort key
    for number, (gsi_pk_template, gsi_sk_template) in enumerate(global_indexes, 1):
        partition = KeyPair(f"GSI{number}PK", gsi_pk_template)
        sort = KeyPair(f"GSI{number}SK", gsi_sk_template)
        indexes.append(GlobalSecondaryIndex(f"GSI{number}", partition, sort))

    return indexes
