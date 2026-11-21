"""Query comparators, and the filters and key conditions they build.

Comparators are passed to `DynamoModel.query` in place of plain (equality) values, e.g.
`Order.query({"customer_id": "c1", "date": Between("2024-01", "2024-03")})`.

Each comparator builds a filter on an attribute with `filter`, and those that can narrow
a key build a key condition with `create_key_condition_expression`. `Equal`, `Between`,
`BeginsWith`, `GreaterThan`, `GreaterThanOrEqual`, `LessThan` and `LessThanOrEqual` can
narrow a key; `NotEqual`, `In`, `Contains`, `Exists` and `NotExists` are filter-only.
"""

import dataclasses
from collections.abc import Mapping
from typing import Any, ClassVar

from boto3.dynamodb.conditions import Attr, ConditionBase, Key

from .indexes import KeyPair

# Highest code point: `text + _MAX_CHAR` sorts after every key starting with `text`
_MAX_CHAR = "\U0010ffff"

# Lowest code point: `text + _MIN_CHAR` is the first key after `text`
_MIN_CHAR = "\x00"


def _before(text: str) -> str:
    """Get the highest key below `text`, to end a range that excludes `text`.

    Keys starting with that key and continuing with `_MAX_CHAR` are missed, which
    real keys don't contain.
    """
    # Every key below `text` + `_MIN_CHAR` is at most `text` without it
    if text.endswith(_MIN_CHAR):
        return text[:-1]

    # Lower the last character, skipping the surrogates DynamoDB can't store
    code = ord(text[-1]) - 1
    if 0xD800 <= code <= 0xDFFF:
        code = 0xD7FF
    return text[:-1] + chr(code) + _MAX_CHAR


class Comparator:
    """A condition on a single attribute, used when querying a `DynamoModel`.

    A comparator on an attribute in the middle of a key (e.g. `date` in
    `ORDER#{date}#{order_id}`) matches every key with a matching value, whatever
    follows it. One-sided ranges are capped by the key text before the attribute
    (e.g. `ORDER#`), so they don't read other item types.

    Attributes:
        narrows_key: Whether the comparator can build a key condition
    """

    narrows_key: ClassVar[bool] = False

    def _format(self, key: KeyPair, values: Mapping[str, Any], operand: Any) -> str:
        """Format `key` from `values`, with `operand` in place of this comparator."""
        substituted = {
            name: operand if value is self else value for name, value in values.items()
        }
        return key.format(substituted, partial=True)

    def _placement(self, key: KeyPair, values: Mapping[str, Any]) -> tuple[str, bool]:
        """Get the key text before this comparator's attribute, and whether it ends.

        Returns:
            The text before the attribute, empty for a column key, and whether the
            attribute is the key's last, so its formatted value completes the key
        """
        if key.is_column:
            return "", True
        attribute = next(name for name, value in values.items() if value is self)
        before = {name: value for name, value in values.items() if value is not self}
        return key.format(before, partial=True), attribute == key.attributes[-1]

    def filter(self, attr: Attr) -> ConditionBase:
        """Get the condition on the raw attribute, for a filter expression."""
        raise NotImplementedError

    def create_key_condition_expression(
        self, key: KeyPair, values: Mapping[str, Any]
    ) -> ConditionBase:
        """Get the condition on `key`, formatted from `values`.

        Args:
            key: The key the condition is on
            values: The values the key is formatted from, holding this comparator in
                place of its operand

        Raises:
            NotImplementedError: If the comparator is filter-only
        """
        raise NotImplementedError(f"{type(self).__name__} can't build a key condition")


@dataclasses.dataclass(frozen=True)
class Equal(Comparator):
    """The attribute is equal to `value`, as a plain value is."""

    narrows_key = True

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr = value`."""
        return attr.eq(self.value)

    def create_key_condition_expression(self, key, values):
        """Get `key = value`, or `begins_with` it when more of the key follows."""
        _, is_last = self._placement(key, values)
        value = self._format(key, values, self.value)
        if is_last:
            return Key(key.name).eq(value)
        return Key(key.name).begins_with(value)


@dataclasses.dataclass(frozen=True)
class Between(Comparator):
    """The attribute is between `low` and `high`, inclusive."""

    narrows_key = True

    low: Any
    high: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr BETWEEN low AND high`."""
        return attr.between(self.low, self.high)

    def create_key_condition_expression(self, key, values):
        """Get `key BETWEEN low AND high`, with each bound formatted into the key."""
        _, is_last = self._placement(key, values)
        low = self._format(key, values, self.low)
        high = self._format(key, values, self.high)

        # Include the keys that continue past the upper bound's value
        if not is_last:
            high += _MAX_CHAR
        return Key(key.name).between(low, high)


@dataclasses.dataclass(frozen=True)
class BeginsWith(Comparator):
    """The attribute starts with `prefix`."""

    narrows_key = True

    prefix: str

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `begins_with(attr, prefix)`."""
        return attr.begins_with(self.prefix)

    def create_key_condition_expression(self, key, values):
        """Get `begins_with(key, prefix)`, with the prefix formatted into the key."""
        return Key(key.name).begins_with(self._format(key, values, self.prefix))


@dataclasses.dataclass(frozen=True)
class GreaterThan(Comparator):
    """The attribute is greater than `value`."""

    narrows_key = True

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr > value`."""
        return attr.gt(self.value)

    def create_key_condition_expression(self, key, values):
        """Get the keys after every key for `value`, capped by the text before it."""
        prefix, is_last = self._placement(key, values)
        value = self._format(key, values, self.value)

        # With nothing to cap it, a value that ends the key is its own bound
        if is_last and not prefix:
            return Key(key.name).gt(value)

        # Skip the keys that continue past the value, or only the value at the end
        low = value + (_MIN_CHAR if is_last else _MAX_CHAR)
        if prefix:
            return Key(key.name).between(low, prefix + _MAX_CHAR)
        return Key(key.name).gte(low)


@dataclasses.dataclass(frozen=True)
class GreaterThanOrEqual(Comparator):
    """The attribute is greater than or equal to `value`."""

    narrows_key = True

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr >= value`."""
        return attr.gte(self.value)

    def create_key_condition_expression(self, key, values):
        """Get the keys from the first key for `value`, capped by the text before it."""
        prefix, _ = self._placement(key, values)
        low = self._format(key, values, self.value)
        if prefix:
            return Key(key.name).between(low, prefix + _MAX_CHAR)
        return Key(key.name).gte(low)


@dataclasses.dataclass(frozen=True)
class LessThan(Comparator):
    """The attribute is less than `value`."""

    narrows_key = True

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr < value`."""
        return attr.lt(self.value)

    def create_key_condition_expression(self, key, values):
        """Get the keys before every key for `value`, from the text before it."""
        prefix, _ = self._placement(key, values)
        value = self._format(key, values, self.value)
        if prefix:
            return Key(key.name).between(prefix, _before(value))
        return Key(key.name).lt(value)


@dataclasses.dataclass(frozen=True)
class LessThanOrEqual(Comparator):
    """The attribute is less than or equal to `value`."""

    narrows_key = True

    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        """Get `attr <= value`."""
        return attr.lte(self.value)

    def create_key_condition_expression(self, key, values):
        """Get the keys up to every key for `value`, from the text before it."""
        prefix, is_last = self._placement(key, values)
        high = self._format(key, values, self.value)

        # Include the keys that continue past the value
        if not is_last:
            high += _MAX_CHAR
        if prefix:
            return Key(key.name).between(prefix, high)
        return Key(key.name).lte(high)


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
