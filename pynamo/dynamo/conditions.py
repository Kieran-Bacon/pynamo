"""Query comparators, and the key conditions and filters built from them.

Comparators are passed to `DynamoModel.query` in place of plain (equality) values, e.g.
`Order.query(table, customer_id="c1", date=between("2024-01", "2024-03"))`.

DynamoDB allows only an equality on the partition key and one condition on the sort key,
so any value the key conditions don't fully cover is applied as a filter instead.
`between`, `begins_with`, `gt`, `gte`, `lt` and `lte` can narrow the sort key; `ne`,
`is_in`, `contains`, `exists` and `not_exists` are filter-only.
"""

import dataclasses
import functools
from collections.abc import Callable
from typing import Any

from boto3.dynamodb.conditions import Attr, ConditionBase, Key

from .indexes import _formatter, _template_fields

# Highest code point: `prefix + _MAX_CHAR` sorts after every key starting with `prefix`
_MAX_CHAR = "\U0010ffff"


class Comparator:
    """A condition on a single attribute, used when querying a `DynamoModel`."""

    def filter(self, attr: Attr) -> ConditionBase:
        """Get the condition on the raw attribute, for a filter expression."""
        raise NotImplementedError

    def key_condition(
        self,
        key: Key,
        prefix: str,
        fmt: Callable[[Any], str],
        is_last: bool,
    ) -> tuple[ConditionBase | None, bool]:
        """Get the condition on a sort key containing this comparator's attribute.

        Args:
            key: The sort key the condition is on
            prefix: The formatted sort key text before this attribute
            fmt: Formats a value of this attribute as it appears in the key
            is_last: Whether this attribute ends the sort key template

        Returns:
            The key condition, or None if the comparator is filter-only, and whether it
            is exact. An inexact condition matches a superset, so must also be filtered.
        """
        # By default a comparator is filter-only
        return None, False


@dataclasses.dataclass(frozen=True)
class Between(Comparator):
    """The attribute is between `low` and `high`, inclusive."""

    low: Any
    high: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr BETWEEN low AND high`."""
        return attr.between(self.low, self.high)

    def key_condition(self, key, prefix, fmt, is_last):
        """Get a `BETWEEN` on the key, exact only if the attribute ends the key."""
        low = prefix + fmt(self.low)
        high = prefix + fmt(self.high)
        if not is_last:
            # Include the rest of the key that follows the upper bound
            high += _MAX_CHAR
        return key.between(low, high), is_last


@dataclasses.dataclass(frozen=True)
class BeginsWith(Comparator):
    """The attribute starts with `prefix`."""

    prefix: str

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `begins_with(attr, prefix)`."""
        return attr.begins_with(self.prefix)

    def key_condition(self, key, prefix, fmt, is_last):
        """Get an exact `begins_with` on the key, using the prefix unformatted."""
        value = prefix + str(self.prefix)
        if not value:
            # DynamoDB rejects an empty begins_with
            return None, False
        return key.begins_with(value), True


@dataclasses.dataclass(frozen=True)
class GreaterThan(Comparator):
    """The attribute is greater than `value`."""

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr > value`."""
        return attr.gt(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        """Get a lower bound on the key, exact only with no prefix at the key's end."""
        low = prefix + fmt(self.value)
        if prefix:
            # Stay within the prefix; the filter removes values equal to `low`
            return key.between(low, prefix + _MAX_CHAR), False
        if is_last:
            return key.gt(low), True
        # Keys for an equal value extend past `low`, so use `>=` and filter
        return key.gte(low), False


@dataclasses.dataclass(frozen=True)
class GreaterThanOrEqual(Comparator):
    """The attribute is greater than or equal to `value`."""

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr >= value`."""
        return attr.gte(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        """Get an exact lower bound on the key."""
        low = prefix + fmt(self.value)
        if prefix:
            # Stay within the prefix so other item types aren't matched
            return key.between(low, prefix + _MAX_CHAR), True
        return key.gte(low), True


@dataclasses.dataclass(frozen=True)
class LessThan(Comparator):
    """The attribute is less than `value`."""

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr < value`."""
        return attr.lt(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        """Get an upper bound on the key, exact only with no prefix."""
        high = prefix + fmt(self.value)
        if prefix:
            # Stay within the prefix; the filter removes values equal to `high`
            return key.between(prefix, high), False
        return key.lt(high), True


@dataclasses.dataclass(frozen=True)
class LessThanOrEqual(Comparator):
    """The attribute is less than or equal to `value`."""

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr <= value`."""
        return attr.lte(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        """Get an upper bound on the key, exact only if the attribute ends the key."""
        high = prefix + fmt(self.value)
        if not is_last:
            # Include the rest of the key that follows the upper bound
            high += _MAX_CHAR
        if prefix:
            # Stay within the prefix so other item types aren't matched
            return key.between(prefix, high), is_last
        return key.lte(high), is_last


@dataclasses.dataclass(frozen=True)
class NotEqual(Comparator):
    """The attribute is not equal to `value` (filter-only)."""

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr <> value`."""
        return attr.ne(self.value)


@dataclasses.dataclass(frozen=True)
class In(Comparator):
    """The attribute is one of `values` (filter-only)."""

    values: tuple[Any, ...]

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr IN (values)`."""
        return attr.is_in(list(self.values))


@dataclasses.dataclass(frozen=True)
class Contains(Comparator):
    """The attribute (string, set or list) contains `value` (filter-only)."""

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `contains(attr, value)`."""
        return attr.contains(self.value)


@dataclasses.dataclass(frozen=True)
class Exists(Comparator):
    """The attribute is present on the item (filter-only)."""

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attribute_exists(attr)`."""
        return attr.exists()


@dataclasses.dataclass(frozen=True)
class NotExists(Comparator):
    """The attribute is absent from the item (filter-only)."""

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attribute_not_exists(attr)`."""
        return attr.not_exists()


def between(low: Any, high: Any) -> Between:
    """Match an attribute between `low` and `high`, inclusive."""
    return Between(low, high)


def begins_with(prefix: str) -> BeginsWith:
    """Match an attribute starting with `prefix`."""
    return BeginsWith(prefix)


def gt(value: Any) -> GreaterThan:
    """Match an attribute greater than `value`."""
    return GreaterThan(value)


def gte(value: Any) -> GreaterThanOrEqual:
    """Match an attribute greater than or equal to `value`."""
    return GreaterThanOrEqual(value)


def lt(value: Any) -> LessThan:
    """Match an attribute less than `value`."""
    return LessThan(value)


def lte(value: Any) -> LessThanOrEqual:
    """Match an attribute less than or equal to `value`."""
    return LessThanOrEqual(value)


def ne(value: Any) -> NotEqual:
    """Match an attribute not equal to `value` (filter-only)."""
    return NotEqual(value)


def is_in(*values: Any) -> In:
    """Match an attribute equal to one of `values` (filter-only)."""
    return In(values)


def contains(value: Any) -> Contains:
    """Match an attribute (string, set or list) containing `value` (filter-only)."""
    return Contains(value)


def exists() -> Exists:
    """Match items that have the attribute (filter-only)."""
    return Exists()


def not_exists() -> NotExists:
    """Match items without the attribute (filter-only)."""
    return NotExists()


def _format_value(value: Any, conversion: str | None, format_spec: str) -> str:
    """Format a value as a template field would, so bounds sort like the keys."""
    return _formatter.format_field(
        _formatter.convert_field(value, conversion), format_spec
    )


def _partition_key_condition(
    name: str, template: str, values: dict[str, Any]
) -> tuple[ConditionBase, set[str]]:
    """Build the equality condition on a partition key.

    Args:
        name: The partition key attribute name, e.g. `"PK"`
        template: The partition key template
        values: The query's values and comparators

    Returns:
        The key condition and the attributes it accounts for

    Raises:
        ValueError: If a template attribute is missing or given a comparator
    """
    fields = _template_fields(template)

    # Every partition key attribute is needed to build the key
    missing = sorted(fields - values.keys())
    if missing:
        raise ValueError(
            f"Values for {', '.join(missing)} are required to query on {name} "
            f"({template!r})"
        )

    # The partition key can only be matched by equality
    comparators = sorted(f for f in fields if isinstance(values[f], Comparator))
    if comparators:
        raise ValueError(
            f"{', '.join(comparators)} are part of the partition key {name} "
            f"({template!r}) and must be given as plain values, not comparators"
        )

    return Key(name).eq(_formatter.vformat(template, (), values)), fields


def _sort_key_condition(
    name: str, template: str, values: dict[str, Any]
) -> tuple[ConditionBase | None, set[str]]:
    """Build the most selective single condition DynamoDB allows on a sort key.

    Plain values fix the key text in template order. The first attribute that is
    missing or has a comparator ends the condition: a comparator becomes a range, and a
    missing attribute a `begins_with` on the text so far. Later attributes are filtered.

    Args:
        name: The sort key attribute name, e.g. `"SK"`
        template: The sort key template
        values: The query's values and comparators

    Returns:
        The condition (None if nothing is fixed) and the attributes it fully accounts for
    """
    key = Key(name)
    parts = list(_formatter.parse(template))
    prefix = ""
    consumed: set[str] = set()

    def prefix_condition() -> tuple[ConditionBase | None, set[str]]:
        """Match on the key text fixed so far, if any."""
        return (key.begins_with(prefix) if prefix else None), consumed

    for position, (literal, field_name, format_spec, conversion) in enumerate(parts):
        # Literal text is always part of the key
        prefix += literal
        if field_name is None:
            continue

        # The key can't be fixed past a missing attribute
        if field_name not in values:
            return prefix_condition()

        # A plain value fixes this part of the key, so move to the next part
        value = values[field_name]
        if not isinstance(value, Comparator):
            prefix += _format_value(value, conversion, format_spec)
            consumed.add(field_name)
            continue

        # The first comparator is the key's only range, so it ends the condition
        condition, exact = value.key_condition(
            key,
            prefix,
            functools.partial(
                _format_value, conversion=conversion, format_spec=format_spec
            ),
            is_last=position == len(parts) - 1,
        )
        if condition is None:
            # Filter-only comparator, so match on the text fixed so far
            return prefix_condition()
        if exact:
            consumed.add(field_name)
        return condition, consumed

    # Every attribute was given a plain value
    return key.eq(prefix), consumed
