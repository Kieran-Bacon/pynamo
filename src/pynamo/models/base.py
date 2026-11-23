"""The `DynamoModel` base class for pydantic models stored in a DynamoDB table.

Subclasses declare their storage settings in a nested `Meta` class (see `.meta`), whose
`indexes` describe the table's keys (see `.indexes`). They gain `get`, `query` and `scan`
(see `.conditions` for comparators), `update` (see `.updates` for actions) and `attach` /
`checkpoint` for change tracking. Every method takes a table, which defaults to the
`Meta.table` or the one set with `default_table`.
"""

import datetime
import decimal
import functools
import operator
from collections.abc import Callable, Generator, Mapping
from typing import Any, ClassVar, Self

import botocore.exceptions
import pydantic
from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from mypy_boto3_dynamodb.service_resource import Table

from .conditions import BeginsWith, Comparator, Equal
from .meta import DynamoMeta
from .updates import _Fields, _UpdateExpression


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

    Subclasses declare their own `Meta` class, whose `indexes` must include exactly one
    `PrimaryIndex` and only reference attributes the model defines. Both are checked
    when the subclass is defined. E.g.

        class Order(DynamoModel):
            customer_id: str
            order_id: str
            date: str

            class Meta:
                indexes = single_table_indexes(
                    base=("CUSTOMER#{customer_id}", "ORDER#{date}#{order_id}"),
                )

    The declared `Meta` is left as it is; its checked settings are held in `_meta`.

    Attributes:
        _meta: The model's `Meta` settings, checked when the subclass is defined
        _fields: The model's fields, prepared for `update` and for naming attributes
        _query_attributes: The attributes `query` and `scan` can filter on
        _default_table: The table used when none is given, set with `default_table`
        _table: The table the model is attached to, if any
        _checkpoint: The stored item when last checkpointed, which `put` diffs against
    """

    _meta: ClassVar[DynamoMeta]
    _fields: ClassVar[_Fields]
    _query_attributes: ClassVar[frozenset[str]]
    _default_table: ClassVar[Table | None] = None

    _table: Table | None = None
    _checkpoint: dict | None = None

    @classmethod
    def default_table(cls, table: Table | None = None) -> Table:
        """Set and get the table used when a method isn't given one.

        A default applies to the class and its subclasses, unless a subclass sets its
        own, so `DynamoModel.default_table(table)` sets it for every model. A model's
        `Meta.table` sets its default when it is defined. It is separate from the table
        an instance is attached to, which takes precedence.

        Args:
            table: The table to set as the default, or None to only get it

        Raises:
            ValueError: If neither this class nor a parent has a default table
        """
        # Set on this class only, so parents and sibling models keep their defaults
        if table is not None:
            cls._default_table = table

        # Class attribute lookup falls back to the nearest parent's default
        if cls._default_table is None:
            raise ValueError(
                f"{cls.__name__} has no table: pass one, attach the model to one, or "
                "set a default with `Meta.table` or `default_table`"
            )
        return cls._default_table

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        """Check a subclass's `Meta` and prepare what the methods need.

        Raises:
            TypeError: If the subclass doesn't declare its own `Meta`, or `DynamoMeta`
                rejects it
            ValueError: If `DynamoMeta` rejects the subclass's indexes
        """
        super().__pydantic_init_subclass__(**kwargs)

        # Each model declares its own Meta, so it never reuses another model's keys
        declared = vars(cls).get("Meta")
        if not isinstance(declared, type):
            raise TypeError(f"{cls.__name__} must declare its own `class Meta`")
        cls._meta = DynamoMeta(declared, cls)

        cls._fields = _Fields(cls)
        cls._query_attributes = frozenset(cls.model_fields) | frozenset(
            cls.model_computed_fields
        )

        # A Meta table becomes this model's default
        if cls._meta.table is not None:
            cls._default_table = cls._meta.table

    @classmethod
    def _request(
        cls,
        filter_values: Mapping[str, Any],
        index_name: str | None,
        limit: int | None,
        consistent_read: bool,
        start_token: dict | None,
    ) -> dict[str, Any]:
        """Build the request parameters shared by query and scan.

        Raises:
            ValueError: If `limit` isn't positive
            TypeError: If `filter_values` names an attribute the model doesn't store
        """
        # Reject unknown attributes, which would otherwise filter out every item
        unknown = sorted(filter_values.keys() - cls._query_attributes)
        if unknown:
            raise TypeError(
                f"{cls.__name__} has no attribute(s) to query on: {', '.join(unknown)}"
            )

        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")

        request: dict[str, Any] = {"ConsistentRead": consistent_read}
        if index_name is not None:
            request["IndexName"] = index_name

        # Bound and resume the request as DynamoDB does
        if limit is not None:
            request["Limit"] = limit
        if start_token is not None:
            request["ExclusiveStartKey"] = start_token

        return request

    @classmethod
    def _paginate_operation(
        cls,
        table: Table,
        operation: Callable[..., Any],
        request: dict[str, Any],
    ) -> Generator[Page[Self]]:
        """Send `request` to `operation` (`table.query` or `table.scan`) until done.

        Args:
            table: The table the request reads, which returned models are attached to
            operation: The table method to send the request with
            request: The request parameters

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
            yield Page(items=items, errors=errors, token=token)

            # Stop when DynamoDB has no more results, otherwise continue after this page
            if token is None:
                break
            request["ExclusiveStartKey"] = token

    @classmethod
    def _filter_expression(cls, values: Mapping[str, Any]) -> ConditionBase | None:
        """Combine a filter for each value, or None if there are none."""
        filters = []
        for name, value in values.items():
            # Fields may be stored under an alias; other attributes under their own name
            attr = Attr(cls._fields.attributes.get(name, name))

            # Comparators build their own condition; plain values match by equality
            if isinstance(value, Comparator):
                filters.append(value.filter(attr))
            else:
                filters.append(attr.eq(value))

        return functools.reduce(operator.and_, filters) if filters else None

    def _item(self) -> dict[str, Any]:
        """Get the item as stored: the model's attributes plus every generated key.

        Column keys are model fields, so are already among the attributes.
        """
        item = self.model_dump(by_alias=True)
        for key in self._meta.generated_keys:
            # Templates may reference properties as well as fields, so read attributes
            values = {name: getattr(self, name) for name in key.attributes}
            item[key.name] = key.format(values)
        return item

    def _update_arguments(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Build `update_item` arguments for how `item` differs from the checkpoint.

        Returns:
            The arguments, or None if nothing has changed

        Raises:
            ValueError: If the primary key has changed, as DynamoDB can't update it
        """
        checkpoint = self._checkpoint or {}
        key = self._meta.primary_index.stored_key(checkpoint)
        if any(item[name] != value for name, value in key.items()):
            raise ValueError(
                f"{type(self).__name__}'s primary key has changed from {key}; detach "
                "it to put it as a new item"
            )

        # Set what changed since the checkpoint and remove what's gone, if anything
        expression = _UpdateExpression()
        if expression.add_changes(checkpoint, item):
            return {
                "Key": key,
                **expression.arguments(),
                # Fail rather than create a partial item if it has since been deleted
                "ConditionExpression": Attr(
                    self._meta.primary_index.partition.name
                ).exists(),
            }
        return None

    @classmethod
    def get(
        cls, key: Mapping[str, Any] | None = None, *, table: Table | None = None
    ) -> Self:
        """Get an item by its primary key, attached to `table` and check pointed. E.g.

            Post.get({"user_id": "u1", "post_id": "p1"})
            Settings.get()  # A primary key built from no attributes

        Args:
            key: Values for the attributes the primary key is built from, by name, or
                None when the key templates reference no attributes
            table: The table to read from, or None for the default table

        Raises:
            ValueError: If there is no table, DynamoDB rejects the request, or `key`
                is missing a template key attribute or has one the key doesn't use
            KeyError: If no item has the key, or a column key attribute has no value
        """
        table = table or cls.default_table()

        # No key means no attribute values, so a key needing any still raises
        item_key = cls._meta.primary_index.format(key or {})

        try:
            response = table.get_item(Key=item_key)
        except botocore.exceptions.ClientError as exc:
            raise ValueError(
                f"Validation error on table={table} key={item_key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

        # The response has no "Item" when the key doesn't exist
        if "Item" in response:
            new_obj = cls.model_validate(response["Item"])
            new_obj.attach(table)
            new_obj.checkpoint()
            return new_obj
        raise KeyError(
            f"Could not find entry for key {item_key} in dynamo table {table}"
        )

    def put(self, table: Table | None = None, allow_update: bool = True) -> Self:
        """Write this model and its index keys to a table.

        If the model has a checkpoint for the table (e.g. from `get`, `query` or a
        previous `put`), only the attributes changed since are updated, and nothing is
        sent if none have. Otherwise the whole item is put, replacing any existing
        item. Writing to the attached table then checkpoints the model; writing to
        another table is a one-off put that leaves the attachment and checkpoint as
        they are.

        Args:
            table: The table to write to; defaults to the table the model is attached
                to, else the default table
            allow_update: Update only the changes since the checkpoint, when there is
                one; False always puts the whole item

        Returns:
            This model, for chaining

        Raises:
            ValueError: If there is no table to write to, the check pointed primary key
                has changed, or DynamoDB rejects the request (including updating an
                item that no longer exists)
        """
        table = table or self._table or self.default_table()

        item = self._item()
        attached = table is self._table

        try:
            if allow_update and attached and self._checkpoint is not None:
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
            table: The table to delete from; defaults to the table the model is
                attached to, else the default table

        Returns:
            This model, for chaining

        Raises:
            ValueError: If there is no table to delete from, or DynamoDB rejects the request
        """
        table = table or self._table or self.default_table()
        attached = table is self._table

        # Prefer the key the item was stored under to the key it would have now
        if attached and self._checkpoint is not None:
            key = self._meta.primary_index.stored_key(self._checkpoint)
        else:
            key = self._meta.primary_index.stored_key(self._item())

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
    def update(
        cls,
        key: Mapping[str, Any] | None = None,
        changes: Mapping[str, Any] | None = None,
        *,
        table: Table | None = None,
    ) -> Self:
        """Update an existing item by its primary key, without needing its model.

        Changes are validated with their field's type and field validators (but not
        model validators) before anything is sent. A mapping for a field that holds a
        model updates only the keys it names; any other value, including a model
        instance, replaces the attribute. `UpdateAction`s such as `Increment` are
        applied by DynamoDB. Secondary index keys built from changed attributes are
        regenerated. E.g.

            Post.update({"user_id": "u1", "post_id": "p1"}, {"likes": Increment(1)})
            User.update({"user_id": "u1"}, {"identity": {"username": "new"}})
            Settings.update(changes={"theme": "dark"})  # A key built from no attributes

        Args:
            key: Values for the attributes the primary key is built from, by name, or
                None when the key templates reference no attributes
            changes: The new values of the other attributes, by name; required, but
                defaults to None so `key` can be left out
            table: The table to update, or None for the default table

        Returns:
            The updated model, attached to `table` and checkpointed

        Raises:
            TypeError: If a changed field doesn't exist on the model
            ValueError: If there is no table, no changes, a change to a primary key
                attribute, an invalid value, a changed index key can't be regenerated,
                `key` is missing a template key attribute or has one the key doesn't
                use, or DynamoDB rejects the request
            KeyError: If no item has the key, or a column key attribute has no value
        """
        table = table or cls.default_table()
        primary = cls._meta.primary_index

        # No key means no attribute values, so a key needing any still raises
        key = key or {}
        item_key = primary.format(key)

        if not changes:
            raise ValueError(f"Updating {cls.__name__} needs at least one change")

        # DynamoDB can't change an item's primary key in place
        key_changes = sorted(changes.keys() & primary.attributes)
        if key_changes:
            raise ValueError(
                f"{cls.__name__} can't update primary key attribute(s) "
                f"{', '.join(key_changes)}: delete the item and put it under its new key"
            )

        # Validate each change into a SET clause
        expression = _UpdateExpression()
        validated = cls._fields.collect(changes, (), expression)
        known = {**key, **validated}

        # Rewrite generated keys built from a changed attribute; actions and partial
        # changes have no value, so the key raises if it needs one
        for generated in cls._meta.generated_keys:
            if changes.keys() & generated.attributes:
                expression.set((generated.name,), generated.format(known))

        try:
            response = table.update_item(
                Key=item_key,
                **expression.arguments(),
                # Fail rather than create a partial item if there is no item to update
                ConditionExpression=Attr(
                    cls._meta.primary_index.partition.name
                ).exists(),
                ReturnValues="ALL_NEW",
            )
        except botocore.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise KeyError(
                    f"Could not find entry for key {item_key} in dynamo table {table}"
                ) from exc
            raise ValueError(
                f"Validation error on table={table} key={item_key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

        new_obj = cls.model_validate(response["Attributes"])
        new_obj.attach(table)
        new_obj.checkpoint()
        return new_obj

    @classmethod
    def remove(
        cls, key: Mapping[str, Any] | None = None, *, table: Table | None = None
    ) -> None:
        """Delete an item by its primary key, without needing its model. E.g.

            Post.remove({"user_id": "u1", "post_id": "p1"})
            Settings.remove()  # A primary key built from no attributes

        Args:
            key: Values for the attributes the primary key is built from, by name, or
                None when the key templates reference no attributes
            table: The table to delete from, or None for the default table

        Raises:
            ValueError: If there is no table, DynamoDB rejects the request, or `key`
                is missing a template key attribute or has one the key doesn't use
            KeyError: If a column key attribute has no value
        """
        table = table or cls.default_table()

        # No key means no attribute values, so a key needing any still raises
        item_key = cls._meta.primary_index.format(key or {})

        try:
            table.delete_item(Key=item_key)
        except botocore.exceptions.ClientError as exc:
            raise ValueError(
                f"Validation error on table={table} key={item_key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

    @classmethod
    def query(
        cls,
        values: Mapping[str, Any],
        index_name: str | None = None,
        *,
        table: Table | None = None,
        limit: int | None = None,
        scan_forward: bool = True,
        consistent_read: bool = False,
        start_token: dict | None = None,
    ) -> Generator[Page[Self]]:
        """Query the table, yielding a page per DynamoDB request.

        Values for the index's partition key attributes are required. Sort key values
        narrow the key condition, in template order, up to and including the first
        comparator; the rest become filters. Items that fail validation are put in the
        page's `errors` rather than raised. E.g.

            for page in Order.query({"customer_id": "c1", "status": "open"}):
                for order in page.items:
                    ...
                for invalid in page.errors:
                    ...

        Args:
            values: Attribute values or comparators to query on, by attribute name
            index_name: A secondary index to query, or None for the primary index
            table: The table to query, or None for the default table; returned items
                are attached to it
            limit: DynamoDB's `Limit`, applied to each request
            scan_forward: Return items in ascending sort key order
            consistent_read: Use strongly consistent reads
            start_token: A page's token to resume the query from

        Raises:
            ValueError: If there is no table, `limit` isn't positive, the index doesn't
                exist, or a partition key value is missing or a comparator
            TypeError: If `values` names an attribute the model doesn't have
        """
        table = table or cls.default_table()
        partition, sort = cls._meta.get_keys(index_name)

        # The partition key must be an exact value, so needs a plain value for each of
        # its attributes
        partition_values, remaining = partition.extract_fields(values, required=True)
        if any(isinstance(value, Comparator) for value in partition_values.values()):
            raise ValueError(
                f"{cls.__name__}'s partition key attributes must be plain values, not "
                "comparators"
            )

        # Take the sort key values given, in template order
        sort_values, filter_values = sort.extract_fields(remaining, required=False)

        # The first comparator ends the key condition, and without one the sort key
        # begins with the values given; a filter-only comparator is left to the filter
        ordered = list(sort_values.items())
        default = BeginsWith(prefix="")
        comparator: Comparator = default
        split = len(ordered)
        for i, (_, value) in enumerate(ordered):
            if isinstance(value, Comparator):
                split = i + 1 if value.narrows_key else i
                comparator = value if value.narrows_key else comparator
                break

        # Move the values after the key condition into the filter
        sort_values = dict(ordered[:split])
        filter_values |= dict(ordered[split:])

        # With no comparator and a plain value for every sort key attribute, the key
        # must equal them
        all_plain = comparator is default and split == len(ordered)
        if all_plain and ordered and len(ordered) == len(sort.attributes):
            last, value = ordered[-1]
            comparator = Equal(value)
            sort_values[last] = comparator

        # Fix the partition key, and narrow the sort key when there's something to
        # match: values for it, or literal text before its first attribute
        key_condition = Key(partition.name).eq(partition.format(partition_values))
        if sort_values or (not sort.is_column and sort.format({}, partial=True)):
            key_condition &= comparator.create_key_condition_expression(
                sort, sort_values
            )

        # Build the request
        request = cls._request(
            filter_values,
            index_name=index_name,
            limit=limit,
            consistent_read=consistent_read,
            start_token=start_token,
        )
        request["ScanIndexForward"] = scan_forward
        request["KeyConditionExpression"] = key_condition
        if filter_values:
            request["FilterExpression"] = cls._filter_expression(filter_values)

        yield from cls._paginate_operation(table, table.query, request)

    @classmethod
    def scan(
        cls,
        values: Mapping[str, Any] | None = None,
        index_name: str | None = None,
        *,
        table: Table | None = None,
        limit: int | None = None,
        consistent_read: bool = False,
        start_token: dict | None = None,
    ) -> Generator[Page[Self]]:
        """Scan the table, yielding a page per DynamoDB request.

        Items are filtered to those whose partition and sort keys start with the
        literal text of their templates (e.g. `CUSTOMER#` and `ORDER#`), and every
        value becomes a filter. Items that fail validation are put in the page's
        `errors` rather than raised. E.g.

            for page in Order.scan({"status": "open"}):
                for order in page.items:
                    ...
                for invalid in page.errors:
                    ...

        Args:
            values: Attribute values or comparators to filter on, by attribute name
            index_name: A secondary index to scan, or None for the table
            table: The table to scan, or None for the default table; returned items
                are attached to it
            limit: DynamoDB's `Limit`, applied to each request
            consistent_read: Use strongly consistent reads
            start_token: A page's token to resume the scan from

        Raises:
            ValueError: If there is no table, `limit` isn't positive or the index
                doesn't exist
            TypeError: If `values` names an attribute the model doesn't have
        """
        table = table or cls.default_table()
        values = values or {}
        request = cls._request(values, index_name, limit, consistent_read, start_token)

        # Match this model's items by the literal text each key starts with; column
        # keys and templates starting with an attribute have none
        filters = []
        for key in cls._meta.get_keys(index_name):
            prefix = "" if key.is_column else key.format({}, partial=True)
            if prefix:
                filters.append(Attr(key.name).begins_with(prefix))

        # With no key condition, every value is also a filter
        value_filter = cls._filter_expression(values)
        if value_filter is not None:
            filters.append(value_filter)
        if filters:
            request["FilterExpression"] = functools.reduce(operator.and_, filters)

        yield from cls._paginate_operation(table, table.scan, request)

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
