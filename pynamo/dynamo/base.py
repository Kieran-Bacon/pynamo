"""The `DynamoModel` base class for pydantic models stored in a DynamoDB table.

Subclasses declare their table's `indexes` (see `.indexes`) and gain `get`, `query`
(see `.conditions` for comparators) and `attach` / `checkpoint` for change tracking.
"""

import datetime
import decimal
import functools
import operator
from collections.abc import Generator
from typing import Any, ClassVar, Self

import botocore.exceptions
import pydantic
from boto3.dynamodb.conditions import Attr
from mypy_boto3_dynamodb.service_resource import Table

from .conditions import Comparator, _partition_key_condition, _sort_key_condition
from .indexes import (
    GlobalSecondaryIndex,
    Index,
    LocalSecondaryIndex,
    PrimaryIndex,
    _template_fields,
)


class Page[T: pydantic.BaseModel](pydantic.BaseModel):
    """A page of query results.

    Attributes:
        items: The models returned by the request
        token: Pass as `start_token` to continue the query; None when there are no more
    """

    items: list[T]
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
        _table: The table the model is attached to, if any
        _checkpoint: The model's state when last checkpointed, for change tracking
    """

    indexes: ClassVar[list[Index]]
    _primary_index: ClassVar[PrimaryIndex]

    _table: Table | None = None
    _checkpoint: dict | None = None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        """Validate a subclass's `indexes` against each other and the model.

        Raises:
            TypeError: If `indexes` is not a list or holds an unsupported index type
            ValueError: If there isn't exactly one `PrimaryIndex`, secondary index names
                repeat, or a key template references an undefined attribute
        """
        super().__pydantic_init_subclass__(**kwargs)

        indexes = getattr(cls, "indexes", None)
        if not isinstance(indexes, list):
            raise TypeError(f"{cls.__name__}.indexes must be a list of indexes")

        # Find the primary index and collect every key template to check
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
                # Local indexes only have their own sort key
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

        # Every key attribute must be a field or other attribute (e.g. a property)
        for key_name, key_format in templates:
            for field_name in sorted(_template_fields(key_format)):
                if field_name not in cls.model_fields and not hasattr(cls, field_name):
                    raise ValueError(
                        f"{key_name} format references {field_name!r}, "
                        f"which is not defined on {cls.__name__}"
                    )

        cls._primary_index = primary_index

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
        index_name: str | None = None,
        *,
        limit: int | None = None,
        scan_forward: bool = True,
        consistent_read: bool = False,
        start_token: dict | None = None,
        **values: Any,
    ) -> Generator[Page[Self]]:
        """Query the table, yielding a page per DynamoDB request.

        Values for every partition key attribute are required. Sort key attributes fix
        the key in template order until the first comparator, which becomes the key's
        range. All other values, including any further sort key conditions, are filters.
        E.g. with the sort key `"ORDER#{date}#{order_id}"`:

            Order.query(table, customer_id="c1", date=between("2024-01", "2024-03"))

        Errors are raised when called; DynamoDB is only called as pages are iterated.

        Args:
            table: The table to query; returned items are attached to it
            index_name: A secondary index to query, or None for the primary index
            limit: The maximum number of items to return in total
            scan_forward: Return items in ascending sort key order
            consistent_read: Use strongly consistent reads (not supported by GSIs)
            start_token: A previous page's token to continue from
            **values: Attribute values or comparators to query on

        Raises:
            ValueError: If `limit` isn't positive, the index doesn't exist, or a
                partition key value is missing
            TypeError: If `values` names an attribute the model doesn't have
        """

        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")

        # Reject unknown attributes, which would otherwise filter out every item
        known = set(cls.model_fields) | set(cls.model_computed_fields)
        for index in cls.indexes:
            known |= _template_fields(getattr(index, "pk_template", ""))
            known |= _template_fields(index.sk_template)

        unknown = sorted(values.keys() - known)

        if unknown:
            raise TypeError(
                f"{cls.__name__} has no attribute(s) to query on: {', '.join(unknown)}"
            )

        # Build the request to DynamoDB
        request: dict[str, Any] = {
            "ScanIndexForward": scan_forward,
            "ConsistentRead": consistent_read,
        }

        # Find the partition key the index is queried on
        index = cls._index(index_name)
        if isinstance(index, LocalSecondaryIndex):
            # Local indexes share the table's partition key
            pk_name = cls._primary_index.pk_name
            pk_template = cls._primary_index.pk_template
        else:
            pk_name, pk_template = index.pk_name, index.pk_template
        if not isinstance(index, PrimaryIndex):
            request["IndexName"] = index.index_name

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
    def _index(cls, index_name: str | None) -> Index:
        """Get a secondary index by name, or the primary index for None.

        Raises:
            ValueError: If the model has no index with the name
        """
        if index_name is None:
            return cls._primary_index
        for index in cls.indexes:
            # The primary index has no name, so never matches
            if getattr(index, "index_name", None) == index_name:
                return index
        raise ValueError(f"{cls.__name__} has no index named {index_name!r}")

    @classmethod
    def _attribute_name(cls, name: str) -> str:
        """Get the table attribute name for a model attribute, honouring field aliases."""
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
        """Repeat a query request, yielding a page of attached models per response.

        Args:
            table: The table to query
            request: `Table.query` arguments, updated in place for each request
            limit: The maximum number of items to return in total, or None for all
            token: The key to start from, or None to start at the beginning
        """
        remaining = limit
        while True:
            # Continue from where the previous page ended
            if token is not None:
                request["ExclusiveStartKey"] = token
            if remaining is not None:
                # DynamoDB limits before filtering, so keep requesting until enough
                request["Limit"] = remaining

            response = table.query(**request)

            # Load each item as an attached, checkpointed model
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

            # Stop when DynamoDB has no more results or the limit is reached
            if token is None or (remaining is not None and remaining <= 0):
                return

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
