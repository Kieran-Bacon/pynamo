"""The `DynamoModel` base class for pydantic models stored in a DynamoDB table.

Subclasses declare their table's `indexes` (see `.indexes`) and gain `get`, `query` and
`scan` (see `.conditions` for comparators) and `attach` / `checkpoint` for change
tracking.
"""

import datetime
import decimal
import functools
import operator
from collections.abc import Callable, Generator, Mapping
from typing import Any, ClassVar, Self

import botocore.exceptions
import pydantic
from boto3.dynamodb.conditions import Attr, ConditionBase
from mypy_boto3_dynamodb.service_resource import Table

from .conditions import (
    Comparator,
    _partition_key_condition,
    _prefix_filter,
    _sort_key_condition,
)
from .indexes import (
    GlobalSecondaryIndex,
    Index,
    LocalSecondaryIndex,
    PrimaryIndex,
    _template_fields,
)


class InvalidItem(pydantic.BaseModel):
    """A returned item that failed to validate as the queried model.

    Attributes:
        item: The item as DynamoDB returned it
        error: Why it failed to validate
    """

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)

    item: dict[str, Any]
    error: pydantic.ValidationError


class Page[T: pydantic.BaseModel](pydantic.BaseModel):
    """A page of query results.

    Attributes:
        items: The models returned by the request
        errors: The returned items that failed to validate as the model
        token: Pass as `start_token` to continue the query; None when there are no more
    """

    items: list[T]
    errors: list[InvalidItem] = []
    token: dict | None = None


class DynamoModel(pydantic.BaseModel):
    """Base class for models stored in a DynamoDB table.

    Subclasses set `indexes`, which must include exactly one `PrimaryIndex` and only
    reference attributes the model defines. Both are checked when the subclass is
    defined. E.g.

        class Order(DynamoModel):
            indexes = single_table_indexes(
                base=("CUSTOMER#{customer_id}", "ORDER#{date}#{order_id}"),
            )

            customer_id: str
            order_id: str
            date: str

    Attributes:
        indexes: The table's index definitions
        _primary_index: The `PrimaryIndex` from `indexes`
        _indexes: Each index by name, with the primary index under None
        _query_attributes: The attributes `query` and `scan` accept values for
        _table: The table the model is attached to, if any
        _checkpoint: The model's state when last checkpointed, for change tracking
    """

    indexes: ClassVar[list[Index]]
    _primary_index: ClassVar[PrimaryIndex]
    _indexes: ClassVar[dict[str | None, Index]]
    _query_attributes: ClassVar[frozenset[str]]

    _table: Table | None = None
    _checkpoint: dict | None = None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        """Validate a subclass's `indexes` and cache what `query` needs from them.

        Raises:
            TypeError: If `indexes` is not a list or holds an unsupported index type
            ValueError: If there isn't exactly one `PrimaryIndex`, secondary index names
                repeat, or a key template references an undefined attribute
        """
        super().__pydantic_init_subclass__(**kwargs)

        indexes = getattr(cls, "indexes", None)
        if not isinstance(indexes, list):
            raise TypeError(f"{cls.__name__}.indexes must be a list of indexes")

        # Map each index by name (None for the primary) and collect its key templates
        by_name: dict[str | None, Index] = {}
        primary_index: PrimaryIndex | None = None
        templates = []
        for index in indexes:
            if isinstance(index, PrimaryIndex):
                name = None
                primary_index = index
                templates.append((index.pk_name, index.pk_template))
            elif isinstance(index, LocalSecondaryIndex):
                # Local indexes only have their own sort key
                name = index.index_name
            elif isinstance(index, GlobalSecondaryIndex):
                name = index.index_name
                templates.append((index.pk_name, index.pk_template))
            else:
                raise TypeError(f"Unsupported index type: {type(index).__name__}")
            templates.append((index.sk_name, index.sk_template))

            if name in by_name:
                raise ValueError(
                    f"{cls.__name__}.indexes must contain exactly one PrimaryIndex"
                    if name is None
                    else f"{cls.__name__}.indexes must have unique index names"
                )
            by_name[name] = index

        if primary_index is None:
            raise ValueError(f"{cls.__name__}.indexes must include a PrimaryIndex")

        # Every key attribute must be a field or other attribute (e.g. a property)
        key_attributes: set[str] = set()
        for key_name, key_format in templates:
            for field_name in sorted(_template_fields(key_format)):
                if field_name not in cls.model_fields and not hasattr(cls, field_name):
                    raise ValueError(
                        f"{key_name} format references {field_name!r}, "
                        f"which is not defined on {cls.__name__}"
                    )
                key_attributes.add(field_name)

        cls._primary_index = primary_index
        cls._indexes = by_name
        cls._query_attributes = frozenset(
            key_attributes | set(cls.model_fields) | set(cls.model_computed_fields)
        )

    @classmethod
    def _attribute_name(cls, name: str) -> str:
        """Get the table attribute name for a model attribute, honouring field aliases."""
        field = cls.model_fields.get(name)
        if field is not None:
            return field.serialization_alias or field.alias or name
        return name

    @classmethod
    def _request(
        cls,
        values: Mapping[str, Any],
        index_name: str | None,
        limit: int | None,
        consistent_read: bool,
        start_token: dict | None,
    ) -> tuple[dict[str, Any], Index]:
        """Build the request parameters shared by query and scan.

        Returns:
            The request and the index it reads

        Raises:
            ValueError: If `limit` isn't positive or the index doesn't exist
            TypeError: If `values` names an attribute the model doesn't have
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")

        # Reject unknown attributes, which would otherwise filter out every item
        unknown = sorted(values.keys() - cls._query_attributes)
        if unknown:
            raise TypeError(
                f"{cls.__name__} has no attribute(s) to query on: {', '.join(unknown)}"
            )

        index = cls._indexes.get(index_name)
        if index is None:
            raise ValueError(f"{cls.__name__} has no index named {index_name!r}")

        request: dict[str, Any] = {"ConsistentRead": consistent_read}
        if not isinstance(index, PrimaryIndex):
            request["IndexName"] = index.index_name

        # Bound and resume the request as DynamoDB does
        if limit is not None:
            request["Limit"] = limit
        if start_token is not None:
            request["ExclusiveStartKey"] = start_token
        return request, index

    @classmethod
    def _filter_expression(
        cls, values: Mapping[str, Any], consumed: set[str]
    ) -> ConditionBase | None:
        """Combine a filter for each value not in `consumed`, or None if there are none."""
        filters = []
        for name, value in values.items():
            if name in consumed:
                continue
            # Comparators build their own condition; plain values match by equality
            attr = Attr(cls._attribute_name(name))
            if isinstance(value, Comparator):
                filters.append(value.filter(attr))
            else:
                filters.append(attr.eq(value))
        return functools.reduce(operator.and_, filters) if filters else None

    @classmethod
    def _pages(
        cls,
        table: Table,
        operation: Callable[..., Any],
        request: dict[str, Any],
    ) -> Generator[Page[Self]]:
        """Send `request` to `operation` (`table.query` or `table.scan`) until done.

        Yields:
            A page per request, with valid items attached to `table` and checkpointed
        """
        while True:
            response = operation(**request)

            # Load each item as an attached, checkpointed model, collecting any failures
            items: list[Self] = []
            errors: list[InvalidItem] = []
            for item in response.get("Items", []):
                try:
                    model = cls.model_validate(item)
                except pydantic.ValidationError as error:
                    errors.append(InvalidItem(item=item, error=error))
                    continue

                model.attach(table)
                model.checkpoint()
                items.append(model)

            token = response.get("LastEvaluatedKey")

            # Yield the page
            yield Page(items=items, errors=errors, token=token)

            # Stop when DynamoDB has no more results, otherwise continue after this page
            if token is None:
                break

            request["ExclusiveStartKey"] = token

    @classmethod
    def get(cls, table: Table, **kwargs) -> Self:
        """Get an item by its primary key, attached to `table` and checkpointed.

        Args:
            table: The table to read from
            **kwargs: Values for the attributes in the primary key templates

        Raises:
            ValueError: If DynamoDB rejects the request
            KeyError: If no item has the key, or a key attribute has no value
        """
        # Generate the primary key from the given values
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
            # The response has no "Item" when the key doesn't exist
            raise KeyError(
                f"Could not find entry for key {key} in dynamo table {table}"
            ) from exc

    @classmethod
    def query(
        cls,
        table: Table,
        values: Mapping[str, Any],
        index_name: str | None = None,
        *,
        limit: int | None = None,
        scan_forward: bool = True,
        consistent_read: bool = False,
        start_token: dict | None = None,
    ) -> Generator[Page[Self]]:
        """Query the table, yielding a page per DynamoDB request.

        Values for the index's partition key attributes are required. Sort key values
        narrow the key condition; the rest become filters. Items that fail validation
        are put in the page's `errors` rather than raised. E.g.

            for page in Order.query(table, {"customer_id": "c1", "status": "open"}):
                for order in page.items:
                    ...
                for invalid in page.errors:
                    ...

        Args:
            table: The table to query; returned items are attached to it
            values: Attribute values or comparators to query on, by attribute name
            index_name: A secondary index to query, or None for the primary index
            limit: DynamoDB's `Limit`, applied to each request
            scan_forward: Return items in ascending sort key order
            consistent_read: Use strongly consistent reads
            start_token: A page's token to resume the query from

        Raises:
            ValueError: If `limit` isn't positive, the index doesn't exist, or a
                partition key value is missing
            TypeError: If `values` names an attribute the model doesn't have
        """
        request, index = cls._request(
            values, index_name, limit, consistent_read, start_token
        )
        request["ScanIndexForward"] = scan_forward

        # Find the partition key the index is queried on
        if isinstance(index, LocalSecondaryIndex):
            # Local indexes share the table's partition key
            pk_name = cls._primary_index.pk_name
            pk_template = cls._primary_index.pk_template
        else:
            pk_name, pk_template = index.pk_name, index.pk_template

        # Build the key condition, noting which values it fully covers
        key_condition, consumed = _partition_key_condition(pk_name, pk_template, values)
        sort_condition, sort_consumed = _sort_key_condition(
            index.sk_name, index.sk_template, values
        )
        consumed |= sort_consumed
        if sort_condition is not None:
            key_condition = key_condition & sort_condition
        request["KeyConditionExpression"] = key_condition

        # Filter on every value the key condition doesn't cover
        filter_expression = cls._filter_expression(values, consumed)
        if filter_expression is not None:
            request["FilterExpression"] = filter_expression

        yield from cls._pages(table, table.query, request)

    @classmethod
    def scan(
        cls,
        table: Table,
        values: Mapping[str, Any] | None = None,
        index_name: str | None = None,
        *,
        limit: int | None = None,
        consistent_read: bool = False,
        start_token: dict | None = None,
    ) -> Generator[Page[Self]]:
        """Scan the table, yielding a page per DynamoDB request.

        Items are filtered to those whose primary keys start with the literal text of
        the key templates (e.g. `CUSTOMER#`), and every value becomes a filter. Items
        that fail validation are put in the page's `errors` rather than raised. E.g.

            for page in Order.scan(table, {"status": "open"}):
                for order in page.items:
                    ...
                for invalid in page.errors:
                    ...

        Args:
            table: The table to scan; returned items are attached to it
            values: Attribute values or comparators to filter on, by attribute name
            index_name: A secondary index to scan, or None for the table
            limit: DynamoDB's `Limit`, applied to each request
            consistent_read: Use strongly consistent reads
            start_token: A page's token to resume the scan from

        Raises:
            ValueError: If `limit` isn't positive or the index doesn't exist
            TypeError: If `values` names an attribute the model doesn't have
        """
        values = values or {}
        request, _ = cls._request(
            values, index_name, limit, consistent_read, start_token
        )

        # Match this model's items by their primary key prefixes, which indexes project
        primary = cls._primary_index
        filters = [
            condition
            for condition in (
                _prefix_filter(primary.pk_name, primary.pk_template),
                _prefix_filter(primary.sk_name, primary.sk_template),
                # With no key condition, every value is a filter
                cls._filter_expression(values, set()),
            )
            if condition is not None
        ]
        if filters:
            request["FilterExpression"] = functools.reduce(operator.and_, filters)

        yield from cls._pages(table, table.scan, request)

    def attach(self, table: Table | None) -> Self:
        """Attach this model to a table, or detach it (dropping its checkpoint) with None.

        Returns:
            This model, for chaining
        """
        self._table = table
        if table is None:
            self._checkpoint = None
        return self

    def checkpoint(self):
        """Record the model's current state, against which `update()` finds changes."""
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
