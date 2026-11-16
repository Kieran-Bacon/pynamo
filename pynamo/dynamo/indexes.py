"""Index definitions describing how a table's keys are generated from model attributes.

Keys are `str.format` templates, e.g. `"USER#{user_id}"`. Template fields must name
top-level model attributes: nested fields such as `{owner.id}` are rejected, as
supporting them adds a lot of complexity for little benefit. Expose a nested value as a
top-level field or property instead. Format specs such as `{timestamp:010d}` are allowed.
"""

import string
from collections.abc import Sequence

import pydantic

# Shared formatter for parsing and formatting key templates
_formatter = string.Formatter()


def _template_fields(template: str) -> set[str]:
    """Get the names of the attributes referenced by a key template.

    Args:
        template: A key template, e.g. `"ORDER#{date}#{order_id}"`

    Returns:
        The referenced attribute names, e.g. `{"date", "order_id"}`
    """
    return {
        field_name
        for _, field_name, _, _ in _formatter.parse(template)
        # Literal-only segments have no field name
        if field_name is not None
    }


def _validate_template(template: str) -> str:
    """Check that every field of a key template names a top-level attribute.

    Raises:
        ValueError: If a field is positional (`{}`, `{0}`) or nested (`{owner.id}`)
    """
    for field_name in _template_fields(template):
        # Anything but a plain identifier is positional or selects a nested value
        if not field_name.isidentifier():
            raise ValueError(
                f"Key template {template!r} has the field {{{field_name}}}: key "
                "template fields must name a top-level attribute of the model"
            )
    return template


class _IndexModel(pydantic.BaseModel):
    """Immutable, strictly validated base for index definitions."""

    model_config = pydantic.ConfigDict(frozen=True, strict=True)

    @pydantic.field_validator("pk_template", "sk_template", check_fields=False)
    @classmethod
    def _check_template(cls, template: str) -> str:
        """Validate each key template of the index with `_validate_template`."""
        return _validate_template(template)


class PrimaryIndex(_IndexModel):
    """The table's primary key.

    Attributes:
        pk_name: The partition key attribute name, e.g. `"PK"`
        sk_name: The sort key attribute name, e.g. `"SK"`
        pk_template: The template the partition key is generated from
        sk_template: The template the sort key is generated from
    """

    pk_name: str = pydantic.Field(min_length=1)
    sk_name: str = pydantic.Field(min_length=1)
    pk_template: str = pydantic.Field(min_length=1)
    sk_template: str = pydantic.Field(min_length=1)


class LocalSecondaryIndex(_IndexModel):
    """An alternative sort key that shares the primary index's partition key.

    Attributes:
        index_name: The index name, e.g. `"LSI1"`
        sk_name: The sort key attribute name, e.g. `"LSI1SK"`
        sk_template: The template the sort key is generated from
    """

    index_name: str = pydantic.Field(min_length=1)
    sk_name: str = pydantic.Field(min_length=1)
    sk_template: str = pydantic.Field(min_length=1)


class GlobalSecondaryIndex(_IndexModel):
    """An alternative partition and sort key.

    Attributes:
        index_name: The index name, e.g. `"GSI1"`
        pk_name: The partition key attribute name, e.g. `"GSI1PK"`
        sk_name: The sort key attribute name, e.g. `"GSI1SK"`
        pk_template: The template the partition key is generated from
        sk_template: The template the sort key is generated from
    """

    index_name: str = pydantic.Field(min_length=1)
    pk_name: str = pydantic.Field(min_length=1)
    sk_name: str = pydantic.Field(min_length=1)
    pk_template: str = pydantic.Field(min_length=1)
    sk_template: str = pydantic.Field(min_length=1)


type Index = PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex
"""Any of the supported index definitions"""


def single_table_indexes(
    base: tuple[str, str],
    local: Sequence[str] = (),
    global_indexes: Sequence[tuple[str, str]] = (),
) -> list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex]:
    """Create a model's indexes using single-table naming conventions.

    Keys are named `PK`/`SK`, `LSI<n>SK` and `GSI<n>PK`/`GSI<n>SK`, numbered in the
    order the indexes are given.

    Args:
        base: The primary index's partition and sort key templates
        local: Each local secondary index's sort key template
        global_indexes: Each global secondary index's partition and sort key templates

    Returns:
        The primary index followed by the local and then the global indexes

    Raises:
        ValueError: If two secondary indexes would share a name
    """
    pk_template, sk_template = base

    # The primary index always comes first
    indexes: list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex] = [
        PrimaryIndex(
            pk_name="PK",
            sk_name="SK",
            pk_template=pk_template,
            sk_template=sk_template,
        )
    ]

    # Local indexes only define a sort key
    for number, definition in enumerate(local, start=1):
        index = LocalSecondaryIndex(
            index_name=f"LSI{number}",
            sk_name=f"LSI{number}SK",
            sk_template=definition,
        )
        indexes.append(index)

    # Global indexes define both a partition and sort key
    for number, definition in enumerate(global_indexes, start=1):
        gsi_pk_template, gsi_sk_template = definition
        index = GlobalSecondaryIndex(
            index_name=f"GSI{number}",
            pk_name=f"GSI{number}PK",
            sk_name=f"GSI{number}SK",
            pk_template=gsi_pk_template,
            sk_template=gsi_sk_template,
        )
        indexes.append(index)

    # Secondary indexes are looked up by name, so names must be unique
    index_names = [
        index.index_name
        for index in indexes
        if isinstance(index, (LocalSecondaryIndex, GlobalSecondaryIndex))
    ]
    if len(index_names) != len(set(index_names)):
        raise ValueError("Secondary index names must be unique")

    return indexes
