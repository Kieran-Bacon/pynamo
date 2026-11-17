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
    _formatter,
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
        _key_templates: Each key attribute name and its template, for `put`
        _table: The table the model is attached to, if any
        _checkpoint: The stored item when last checkpointed, which `put` diffs against
    """

    indexes: ClassVar[list[Index]]
    _primary_index: ClassVar[PrimaryIndex]
    _indexes: ClassVar[dict[str | None, Index]]
    _query_attributes: ClassVar[frozenset[str]]
    _key_templates: ClassVar[tuple[tuple[str, str], ...]]

    _table: Table | None = None
    _checkpoint: dict | None = None

    @classmethod
    def default_table(cls, table: Table | None = None) -> Table:
        """Set and get the table to use for this model, or raise if none is given or attached."""
        # TODO update this so that the default table is separate from the instance table
        # this should check this class for the table, if it is not there, it should look
        # in the parent classes for the table. Raise an error if no table is found in the class or any of its parents.
        # Make all the other methods take table optionally and they call this method to get the table to use.

        if table is not None:
            cls._table = table

        if cls._table is None:
            raise ValueError(
                f"{cls.__name__} has no default table; pass one to the method or "
                "attach the model to a table with `attach`"
            )
        return cls._table

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        """Validate a subclass's `indexes` and cache what `query` and `put` need.

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
        cls._key_templates = tuple(templates)

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

    def _keys(self) -> dict[str, str]:
        """Generate every index's key attributes from this model's attributes."""
        keys = {}
        for key_name, template in self._key_templates:
            # Template fields may be properties, so read each from the model
            values = {name: getattr(self, name) for name in _template_fields(template)}
            keys[key_name] = _formatter.vformat(template, (), values)
        return keys

    def _item(self) -> dict[str, Any]:
        """Get the item as stored: the model's attributes plus every index's keys."""
        return self.model_dump(by_alias=True) | self._keys()

    def _update_arguments(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Build `update_item` arguments for how `item` differs from the checkpoint.

        Returns:
            The arguments, or None if nothing has changed

        Raises:
            ValueError: If the primary key has changed, as DynamoDB can't update it
        """
        checkpoint = self._checkpoint or {}
        primary = self._primary_index
        key = {name: checkpoint[name] for name in (primary.pk_name, primary.sk_name)}
        if any(item[name] != value for name, value in key.items()):
            raise ValueError(
                f"{type(self).__name__}'s primary key has changed from {key}; detach "
                "it to put it as a new item"
            )

        # Set changed and new attributes (the sentinel marks absent ones), and remove
        # attributes no longer present
        changed = [
            name for name, value in item.items() if checkpoint.get(name, ...) != value
        ]
        removed = [name for name in checkpoint if name not in item]
        if not changed and not removed:
            return None

        # Use placeholders, as names may be reserved words; boto3 uses #n/:v for the
        # condition, so these use #u/:u
        names = {f"#u{i}": name for i, name in enumerate(changed + removed)}
        values = {f":u{i}": item[name] for i, name in enumerate(changed)}
        clauses = []
        if changed:
            clauses.append(
                "SET " + ", ".join(f"#u{i} = :u{i}" for i in range(len(changed)))
            )
        if removed:
            offset = len(changed)
            clauses.append(
                "REMOVE " + ", ".join(f"#u{offset + i}" for i in range(len(removed)))
            )

        arguments: dict[str, Any] = {
            "Key": key,
            "UpdateExpression": " ".join(clauses),
            # Fail rather than create a partial item if it has since been deleted
            "ConditionExpression": Attr(primary.pk_name).exists(),
            "ExpressionAttributeNames": names,
        }
        if values:
            arguments["ExpressionAttributeValues"] = values
        return arguments

    def _target_table(self, table: Table | None) -> Table:
        """Get the given table, or the attached one if None.

        Raises:
            ValueError: If no table is given and the model isn't attached to one
        """
        if table is None:
            table = self._table
        if table is None:
            raise ValueError(
                f"{type(self).__name__} is not attached to a table, so one must be given"
            )
        return table

    @classmethod
    def _primary_key(cls, values: Mapping[str, Any]) -> dict[str, str]:
        """Generate the primary key from attribute values, for `get` and `remove`.

        Raises:
            KeyError: If a primary key template attribute has no value
        """
        primary = cls._primary_index
        return {
            primary.pk_name: primary.pk_template.format_map(values),
            primary.sk_name: primary.sk_template.format_map(values),
        }

    @classmethod
    def get(cls, table: Table, /, **kwargs) -> Self:
        """Get an item by its primary key, attached to `table` and check pointed.

        Args:
            table: The table to read from; positional-only, so a key attribute can
                also be named `table`
            **kwargs: Values for the attributes in the primary key templates

        Raises:
            ValueError: If DynamoDB rejects the request
            KeyError: If no item has the key, or a key attribute has no value
        """
        key = cls._primary_key(kwargs)

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

    def put(self, table: Table | None = None) -> Self:
        """Write this model and its index keys to a table.

        If the model has a checkpoint for the table (e.g. from `get`, `query` or a
        previous `put`), only the attributes changed since are updated, and nothing is
        sent if none have. Otherwise the whole item is put, replacing any existing
        item. Writing to the attached table then checkpoints the model; writing to
        another table is a one-off put that leaves the attachment and checkpoint as
        they are.

        Args:
            table: The table to write to; defaults to the table the model is attached to

        Returns:
            This model, for chaining

        Raises:
            ValueError: If there is no table to write to, the check pointed primary key
                has changed, or DynamoDB rejects the request (including updating an
                item that no longer exists)
        """
        table = self._target_table(table)

        item = self._item()
        attached = table is self._table

        try:
            if attached and self._checkpoint is not None:
                # Update only what changed since the checkpoint
                arguments = self._update_arguments(item)
                if arguments is not None:
                    table.update_item(**arguments)
            else:
                table.put_item(Item=item)
        except botocore.exceptions.ClientError as exc:
            raise ValueError(
                f"Validation error on table={table} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

        # The attached table now holds this state, so it's the new baseline for changes
        if attached:
            self._checkpoint = item
        return self

    def delete(self, table: Table | None = None) -> Self:
        """Delete this model's item from a table.

        The item is found by the check pointed primary key if the model has a checkpoint
        for the table, so it is deleted even if the key attributes have since changed;
        otherwise by the key generated from the model. Deleting from the attached table
        drops the checkpoint, so a later `put` writes the whole item; deleting from
        another table leaves it as it is.

        Args:
            table: The table to delete from; defaults to the table the model is attached to

        Returns:
            This model, for chaining

        Raises:
            ValueError: If there is no table to delete from, or DynamoDB rejects the request
        """
        table = self._target_table(table)
        attached = table is self._table

        # Prefer the key the item was stored under to the key it would have now
        if attached and self._checkpoint is not None:
            stored = self._checkpoint
        else:
            stored = self._keys()
        primary = self._primary_index
        key = {name: stored[name] for name in (primary.pk_name, primary.sk_name)}

        try:
            table.delete_item(Key=key)
        except botocore.exceptions.ClientError as exc:
            raise ValueError(
                f"Validation error on table={table} key={key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

        # The attached table's item is gone, so there's nothing left to diff against
        if attached:
            self._checkpoint = None
        return self

    @classmethod
    def update(cls, table: Table, /, **kwargs) -> Self:
        """Update an item by its primary key, without needing its model.

        Args:
            table: The table to update; positional-only, so a key attribute can
                also be named `table`
            **kwargs: Values for the attributes in the primary key templates and any
                other attributes to update

        Returns:
            The updated model, attached to `table` and checkpointed

        Raises:
            ValueError: If no attributes are supplied for update or DynamoDB rejects
                the request
            KeyError: If a primary key template attribute has no value
        """
        key = cls._primary_key(kwargs)
        primary = cls._primary_index
        key_attributes = _template_fields(primary.pk_template) | _template_fields(
            primary.sk_template
        )
        updates = {
            name: value for name, value in kwargs.items() if name not in key_attributes
        }
        if not updates:
            raise ValueError("At least one non-primary-key attribute must be supplied")

        # TODO should check that the values being updated are valid according to the model attributes and their validators
        # How does support for updating nested attributes work? Should we support that? If so, how do we handle the attribute names and values in the update expression?
        # TODO to support the required increment feature needed. should be able to specify Model.update(table, user_id="u1", post_id="p1", content="new content", likes=Comparator.increment(1))

        names = {f"#u{i}": name for i, name in enumerate(updates)}
        values = {f":u{i}": value for i, value in enumerate(updates.values())}
        update_expression = "SET " + ", ".join(
            f"#u{i} = :u{i}" for i in range(len(updates))
        )

        try:
            response = table.update_item(
                Key=key,
                UpdateExpression=update_expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            new_obj = cls.model_validate(response["Attributes"])
            new_obj.attach(table)
            new_obj.checkpoint()
            return new_obj
        except botocore.exceptions.ClientError as exc:
            raise ValueError(
                f"Validation error on table={table} key={key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

    @classmethod
    def remove(cls, table: Table, /, **kwargs) -> None:
        """Delete an item by its primary key, without needing its model.

        Args:
            table: The table to delete from; positional-only, so a key attribute can
                also be named `table`
            **kwargs: Values for the attributes in the primary key templates

        Raises:
            ValueError: If DynamoDB rejects the request
            KeyError: If a key attribute has no value
        """
        key = cls._primary_key(kwargs)

        try:
            table.delete_item(Key=key)
        except botocore.exceptions.ClientError as exc:
            raise ValueError(
                f"Validation error on table={table} key={key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
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
        """Attach this model to a table, or detach it with None.

        Changing the table drops the checkpoint, as it only describes the item in the
        table it came from.

        Returns:
            This model, for chaining
        """
        if table is not self._table:
            self._checkpoint = None
        self._table = table
        return self

    def checkpoint(self):
        """Record the model's current stored item, against which `put` finds changes."""
        self._checkpoint = self._item()

    model_config = pydantic.ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True,
        json_encoders={
            datetime.datetime: lambda dt: dt.isoformat(),
            datetime.date: lambda dt: dt.isoformat(),
            decimal.Decimal: lambda d: str(d),
        },
    )
