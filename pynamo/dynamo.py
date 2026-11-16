import dataclasses
import datetime
import decimal
import functools
import operator
import string
from collections.abc import Callable, Generator, Sequence
from typing import Any, ClassVar, Self

import botocore.exceptions
import pydantic
from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from mypy_boto3_dynamodb.service_resource import Table

# The highest code point - used as an upper bound so that a range on a key string
# includes every value that starts with a given prefix
_MAX_CHAR = "\U0010ffff"

_formatter = string.Formatter()


def _root_field(field_name: str) -> str:
    """The attribute a template field refers to e.g. 'user.id' -> 'user'"""
    return field_name.split(".", 1)[0].split("[", 1)[0]


def _template_fields(template: str) -> set[str]:
    return {
        _root_field(field_name)
        for _, field_name, _, _ in _formatter.parse(template)
        if field_name is not None
    }


# ---------------------------------------------------------------------------------------
# Comparators
#
# Passed as keyword values to `DynamoModel.query` in place of a plain value. A plain value
# is treated as an equality condition. Comparators on a sort key attribute are turned into
# the sort key condition where possible, otherwise they become part of the filter.
# ---------------------------------------------------------------------------------------


class Comparator:
    """A condition on a single attribute used when querying a `DynamoModel`"""

    def filter(self, attr: Attr) -> ConditionBase:
        """The condition on the raw attribute, for a filter expression"""
        raise NotImplementedError

    def key_condition(
        self,
        key: Key,
        prefix: str,
        fmt: Callable[[Any], str],
        is_last: bool,
    ) -> tuple[ConditionBase | None, bool]:
        """The condition on a sort key whose text is fixed up to `prefix`, followed by
        this comparator's attribute.

        Args:
            key: The sort key the condition is on
            prefix: The fully formatted sort key text before this attribute
            fmt: Formats a value of this attribute as it appears in the sort key
            is_last: Whether this attribute ends the sort key template

        Returns:
            The key condition (None when the comparator cannot be applied to the sort
            key) and whether it matches the comparator exactly. When it is not exact,
            the key condition matches a superset of the items and the comparator must
            also be applied in the filter.
        """
        return None, False


@dataclasses.dataclass(frozen=True)
class Between(Comparator):
    low: Any
    high: Any

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.between(self.low, self.high)

    def key_condition(self, key, prefix, fmt, is_last):
        low = prefix + fmt(self.low)
        high = prefix + fmt(self.high)
        if not is_last:
            # Allow for the rest of the sort key that follows the upper bound
            high += _MAX_CHAR
        return key.between(low, high), is_last


@dataclasses.dataclass(frozen=True)
class BeginsWith(Comparator):
    prefix: str

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.begins_with(self.prefix)

    def key_condition(self, key, prefix, fmt, is_last):
        value = prefix + str(self.prefix)
        if not value:
            return None, False
        return key.begins_with(value), True


@dataclasses.dataclass(frozen=True)
class GreaterThan(Comparator):
    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.gt(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        low = prefix + fmt(self.value)
        if prefix:
            # Bounded above by the prefix so other item types in the partition are not
            # matched. Between is inclusive, so equal values are removed by the filter
            return key.between(low, prefix + _MAX_CHAR), False
        if is_last:
            return key.gt(low), True
        return key.gte(low), False


@dataclasses.dataclass(frozen=True)
class GreaterThanOrEqual(Comparator):
    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.gte(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        low = prefix + fmt(self.value)
        if prefix:
            return key.between(low, prefix + _MAX_CHAR), True
        return key.gte(low), True


@dataclasses.dataclass(frozen=True)
class LessThan(Comparator):
    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.lt(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        high = prefix + fmt(self.value)
        if prefix:
            # Between is inclusive, so equal values are removed by the filter
            return key.between(prefix, high), False
        return key.lt(high), True


@dataclasses.dataclass(frozen=True)
class LessThanOrEqual(Comparator):
    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.lte(self.value)

    def key_condition(self, key, prefix, fmt, is_last):
        high = prefix + fmt(self.value)
        if not is_last:
            high += _MAX_CHAR
        if prefix:
            return key.between(prefix, high), is_last
        return key.lte(high), is_last


@dataclasses.dataclass(frozen=True)
class NotEqual(Comparator):
    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.ne(self.value)


@dataclasses.dataclass(frozen=True)
class In(Comparator):
    values: tuple[Any, ...]

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.is_in(list(self.values))


@dataclasses.dataclass(frozen=True)
class Contains(Comparator):
    value: Any

    def filter(self, attr: Attr) -> ConditionBase:
        return attr.contains(self.value)


@dataclasses.dataclass(frozen=True)
class Exists(Comparator):
    def filter(self, attr: Attr) -> ConditionBase:
        return attr.exists()


@dataclasses.dataclass(frozen=True)
class NotExists(Comparator):
    def filter(self, attr: Attr) -> ConditionBase:
        return attr.not_exists()


def between(low: Any, high: Any) -> Between:
    """Attribute is between `low` and `high` inclusive"""
    return Between(low, high)


def begins_with(prefix: str) -> BeginsWith:
    """Attribute starts with `prefix`"""
    return BeginsWith(prefix)


def gt(value: Any) -> GreaterThan:
    """Attribute is greater than `value`"""
    return GreaterThan(value)


def gte(value: Any) -> GreaterThanOrEqual:
    """Attribute is greater than or equal to `value`"""
    return GreaterThanOrEqual(value)


def lt(value: Any) -> LessThan:
    """Attribute is less than `value`"""
    return LessThan(value)


def lte(value: Any) -> LessThanOrEqual:
    """Attribute is less than or equal to `value`"""
    return LessThanOrEqual(value)


def ne(value: Any) -> NotEqual:
    """Attribute is not equal to `value` (filter only)"""
    return NotEqual(value)


def is_in(*values: Any) -> In:
    """Attribute is one of `values` (filter only)"""
    return In(values)


def contains(value: Any) -> Contains:
    """Attribute (string, set or list) contains `value` (filter only)"""
    return Contains(value)


def exists() -> Exists:
    """Attribute is present on the item (filter only)"""
    return Exists()


def not_exists() -> NotExists:
    """Attribute is absent from the item (filter only)"""
    return NotExists()


def _format_value(value: Any, conversion: str | None, format_spec: str) -> str:
    return _formatter.format_field(
        _formatter.convert_field(value, conversion), format_spec
    )


def _partition_key_condition(
    name: str, template: str, values: dict[str, Any]
) -> tuple[ConditionBase, set[str]]:
    """Build the equality condition on the partition key"""
    fields = _template_fields(template)
    missing = sorted(fields - values.keys())
    if missing:
        raise ValueError(
            f"Values for {', '.join(missing)} are required to query on {name} "
            f"({template!r})"
        )
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
    """Build the most selective condition on the sort key that DynamoDB can evaluate

    The template is walked in order, fixing the key text for each attribute given as a
    plain value. The first attribute that is missing or has a comparator ends the key
    condition: a comparator becomes the range on the key, and a missing attribute leaves
    a `begins_with` on the text fixed so far. Conditions on any attribute after that
    point can only be applied by the filter.

    Returns:
        The condition (None if no part of the sort key is fixed) and the attributes that
        the condition fully accounts for, which do not need to be filtered on
    """
    key = Key(name)
    parts = list(_formatter.parse(template))
    prefix = ""
    consumed: set[str] = set()

    def prefix_condition():
        return (key.begins_with(prefix) if prefix else None), consumed

    for position, (literal, field_name, format_spec, conversion) in enumerate(parts):
        prefix += literal
        if field_name is None:
            continue

        root = _root_field(field_name)
        if root not in values:
            return prefix_condition()

        value = values[root]
        if not isinstance(value, Comparator):
            field_value, _ = _formatter.get_field(field_name, (), values)
            prefix += _format_value(field_value, conversion, format_spec)
            consumed.add(root)
            continue

        if field_name != root:
            # A comparator cannot be formatted through a nested field reference
            return prefix_condition()

        condition, exact = value.key_condition(
            key,
            prefix,
            functools.partial(
                _format_value, conversion=conversion, format_spec=format_spec
            ),
            is_last=position == len(parts) - 1,
        )
        if condition is None:
            return prefix_condition()
        if exact:
            consumed.add(root)
        return condition, consumed

    return key.eq(prefix), consumed


class _IndexModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True, strict=True)


class PrimaryIndex(_IndexModel):
    pk_name: str = pydantic.Field(min_length=1)
    sk_name: str = pydantic.Field(min_length=1)
    pk_template: str = pydantic.Field(min_length=1)
    sk_template: str = pydantic.Field(min_length=1)


class LocalSecondaryIndex(_IndexModel):
    index_name: str = pydantic.Field(min_length=1)
    sk_name: str = pydantic.Field(min_length=1)
    sk_template: str = pydantic.Field(min_length=1)


class GlobalSecondaryIndex(_IndexModel):
    index_name: str = pydantic.Field(min_length=1)
    pk_name: str = pydantic.Field(min_length=1)
    sk_name: str = pydantic.Field(min_length=1)
    pk_template: str = pydantic.Field(min_length=1)
    sk_template: str = pydantic.Field(min_length=1)


def single_table_indexes(
    base: tuple[str, str],
    local: Sequence[str] = (),
    global_indexes: Sequence[tuple[str, str]] = (),
) -> list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex]:
    """Create a list of indexes for a single-table design"""
    pk_template, sk_template = base

    indexes: list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex] = [
        PrimaryIndex(
            pk_name="PK",
            sk_name="SK",
            pk_template=pk_template,
            sk_template=sk_template,
        )
    ]

    for number, definition in enumerate(local, start=1):
        index = LocalSecondaryIndex(
            index_name=f"LSI{number}",
            sk_name=f"LSI{number}SK",
            sk_template=definition,
        )
        indexes.append(index)

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

    index_names = [
        index.index_name
        for index in indexes
        if isinstance(index, (LocalSecondaryIndex, GlobalSecondaryIndex))
    ]
    if len(index_names) != len(set(index_names)):
        raise ValueError("Secondary index names must be unique")

    return indexes


class Page[T: pydantic.BaseModel](pydantic.BaseModel):
    """A paginated result set from a dynamo query"""

    items: list[T]
    token: dict | None = None


class DynamoModel(pydantic.BaseModel):
    """The base class for models that represent will appear in a dynamoDB table. This
    model base will provide methods for the getting and fetching of the data within the
    table
    """

    indexes: ClassVar[list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex]]
    _primary_index: ClassVar[PrimaryIndex]

    _table: Table | None = None
    _checkpoint: dict | None = None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        super().__pydantic_init_subclass__(**kwargs)

        indexes = getattr(cls, "indexes", None)
        if not isinstance(indexes, list):
            raise TypeError(f"{cls.__name__}.indexes must be a list of indexes")

        formatter = string.Formatter()
        primary_index = None
        templates = []
        index_names = []
        for index in indexes:
            if isinstance(index, PrimaryIndex):
                if primary_index is not None:
                    raise ValueError(
                        f"{cls.__name__}.indexes must contain exactly one PrimaryIndex"
                    )
                primary_index = index
                templates.extend(
                    (
                        (index.pk_name, index.pk_template),
                        (index.sk_name, index.sk_template),
                    )
                )
                continue
            if not isinstance(index, (LocalSecondaryIndex, GlobalSecondaryIndex)):
                raise TypeError(f"Unsupported index type: {type(index).__name__}")
            index_names.append(index.index_name)
            if isinstance(index, LocalSecondaryIndex):
                templates.append((index.sk_name, index.sk_template))
            else:
                templates.extend(
                    (
                        (index.pk_name, index.pk_template),
                        (index.sk_name, index.sk_template),
                    )
                )
        if len(index_names) != len(set(index_names)):
            raise ValueError(f"{cls.__name__}.indexes must have unique index names")
        if primary_index is None:
            raise ValueError(f"{cls.__name__}.indexes must include a PrimaryIndex")

        for key_name, key_format in templates:
            for _, field_name, _, _ in formatter.parse(key_format):
                if field_name is None:
                    continue
                property_name = field_name.split(".", 1)[0].split("[", 1)[0]
                if property_name not in cls.model_fields and not hasattr(
                    cls, property_name
                ):
                    raise ValueError(
                        f"{key_name} format references {property_name!r}, "
                        f"which is not defined on {cls.__name__}"
                    )

        cls._primary_index = primary_index

    @classmethod
    def get(cls, table: Table, **kwargs) -> Self:
        """Get an object from the dynamo table based on the provided key values"""

        primary = cls._primary_index
        key = {
            primary.pk_name: primary.pk_template.format(**kwargs),
            primary.sk_name: primary.sk_template.format(**kwargs),
        }

        try:
            response = table.get_item(Key=key)
            new_obj = cls.model_validate(response["Item"])
            new_obj.attach(table)
            new_obj.checkpoint()
            return new_obj

        except botocore.exceptions.ClientError as exc:
            raise ValueError(
                f"Validation error on table={table} key={key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

        except KeyError as exc:
            raise KeyError(
                f"Could not find entry for key {key} in dynamo table {table}"
            ) from exc

    @classmethod
    def query(
        cls,
        table: Table,
        index_name: str | None = None,
        *,
        limit: int | None = None,
        scan_forward: bool = True,
        consistent_read: bool = False,
        start_token: dict | None = None,
        **values: Any,
    ) -> Generator[Page[Self]]:
        """Query the table for items of this model, yielding a page per request

        Attribute values are given as keywords. Values for every attribute in the
        index's partition key template are required, and build the partition key. The
        sort key condition is built from the sort key template's attributes in order:
        attributes given plain values fix that part of the key, and the first attribute
        given a comparator (e.g. `between`, `gt`, `begins_with`) becomes the range on
        the key. If an attribute is not given, the key is matched with `begins_with` on
        everything fixed before it.

        Every other value is applied as a filter expression on the raw attribute. This
        includes conditions on sort key attributes that cannot be part of the single
        sort key condition, e.g. a second comparator on the sort key.

        E.g. with a sort key template of `"ORDER#{date}#{order_id}"`:

            Order.query(table, customer_id="c1", date=between("2024-01", "2024-03"))

        has the key condition `SK BETWEEN "ORDER#2024-01" AND "ORDER#2024-03\\U0010ffff"`
        and the filter `date BETWEEN "2024-01" AND "2024-03"`.

        Args:
            table: The table to query. Returned items are attached to it
            index_name: The secondary index to query, or None for the primary index
            limit: The maximum number of items to return in total
            scan_forward: Return items in ascending sort key order
            consistent_read: Use strongly consistent reads (not supported by GSIs)
            start_token: The token of a previous page to continue the query from
            **values: Attribute values or comparators to query on

        Returns:
            A generator of pages, one per request made to DynamoDB, each with the token
            to continue the query from. The last page has a token of None.
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")

        known = set(cls.model_fields) | set(cls.model_computed_fields)
        for index in cls.indexes:
            known |= _template_fields(getattr(index, "pk_template", ""))
            known |= _template_fields(index.sk_template)
        unknown = sorted(values.keys() - known)
        if unknown:
            raise TypeError(
                f"{cls.__name__} has no attribute(s) to query on: {', '.join(unknown)}"
            )

        request: dict[str, Any] = {"ScanIndexForward": scan_forward}
        if consistent_read:
            request["ConsistentRead"] = True

        index = cls._index(index_name)
        if isinstance(index, LocalSecondaryIndex):
            # Local indexes share the table's partition key
            pk_name = cls._primary_index.pk_name
            pk_template = cls._primary_index.pk_template
        else:
            pk_name, pk_template = index.pk_name, index.pk_template
        if not isinstance(index, PrimaryIndex):
            request["IndexName"] = index.index_name

        key_condition, consumed = _partition_key_condition(pk_name, pk_template, values)
        sort_condition, sort_consumed = _sort_key_condition(
            index.sk_name, index.sk_template, values
        )
        consumed |= sort_consumed
        if sort_condition is not None:
            key_condition = key_condition & sort_condition
        request["KeyConditionExpression"] = key_condition

        filters = []
        for name, value in values.items():
            if name in consumed:
                continue
            attr = Attr(cls._attribute_name(name))
            if isinstance(value, Comparator):
                filters.append(value.filter(attr))
            else:
                filters.append(attr.eq(value))
        if filters:
            request["FilterExpression"] = functools.reduce(operator.and_, filters)

        return cls._paginate(table, request, limit, start_token)

    @classmethod
    def _index(
        cls, index_name: str | None
    ) -> PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex:
        if index_name is None:
            return cls._primary_index
        for index in cls.indexes:
            if getattr(index, "index_name", None) == index_name:
                return index
        raise ValueError(f"{cls.__name__} has no index named {index_name!r}")

    @classmethod
    def _attribute_name(cls, name: str) -> str:
        """The name the attribute is stored under in the table"""
        field = cls.model_fields.get(name)
        if field is not None:
            return field.serialization_alias or field.alias or name
        return name

    @classmethod
    def _paginate(
        cls,
        table: Table,
        request: dict[str, Any],
        limit: int | None,
        token: dict | None,
    ) -> Generator[Page[Self]]:
        remaining = limit
        while True:
            if token is not None:
                request["ExclusiveStartKey"] = token
            if remaining is not None:
                # DynamoDB applies the limit before filtering, so keep requesting until
                # enough items have been returned or the query is exhausted
                request["Limit"] = remaining

            response = table.query(**request)

            items = []
            for item in response.get("Items", []):
                model = cls.model_validate(item)
                model.attach(table)
                model.checkpoint()
                items.append(model)

            token = response.get("LastEvaluatedKey")
            if remaining is not None:
                remaining -= len(items)

            yield Page[cls](items=items, token=token)

            if token is None or (remaining is not None and remaining <= 0):
                return

    def attach(self, table: Table | None) -> Self:
        """Attach this model to a dynamoDB table"""
        self._table = table
        if table is None:
            self._checkpoint = None
        return self

    def checkpoint(self):
        """Create a new checkpoint for recording updates against

        Records the current state of the object as the checkpoint against which future
        changes will be compared in order to determine how `update()` statements will be
        issued to the database
        """
        self._checkpoint = self.model_dump()

    model_config = pydantic.ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True,
        json_encoders={
            datetime.datetime: lambda dt: dt.isoformat(),
            datetime.date: lambda dt: dt.isoformat(),
            decimal.Decimal: lambda d: str(d),
        },
    )
