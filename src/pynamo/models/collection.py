"""`DynamoCollection`, for querying several models that share a partition in one request.

In single-table designs, different models are often stored under the same partition key,
e.g. a user's profile and their posts under `USER#{user_id}`. A collection queries that
partition once and returns each item as the model it belongs to, which it recognises by
the literal text the models' primary keys start with (e.g. `PROFILE` and `POST#`).
"""

import functools
import operator
from collections.abc import Generator, Mapping, Sequence
from typing import Any, cast

import pydantic
from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from mypy_boto3_dynamodb.service_resource import Table

from .base import DynamoModel, InvalidItem
from .conditions import Comparator


class CollectionPage[T: DynamoModel]:
    """A page of a collection's query results, whose items can be read by model. E.g.

        for page in DynamoCollection(User, Post).query({"user_id": "u1"}):
            for post in page[Post]:
                ...

    Attributes:
        items: Every model returned by the request, in the order DynamoDB returned them
        errors: The returned items that failed to validate as their model
        token: Pass as `start_token` to continue the query; None when there are no more
        _by_model: The items of each model in the collection, by model
    """

    def __init__(
        self,
        models: Sequence[type[T]],
        items: list[T],
        errors: list[InvalidItem],
        token: dict | None,
    ):
        """Hold a request's results, grouping the items by the model they are."""
        self.items = items
        self.errors = errors
        self.token = token

        # Group the items once, so each model's are read directly; every model in the
        # collection has a list, even when the page has none of its items
        self._by_model: dict[type[T], list[T]] = {model: [] for model in models}
        for item in items:
            self._by_model[type(item)].append(item)

    def __getitem__[M: DynamoModel](self, model: type[M]) -> list[M]:
        """Get the page's items of `model`, in the order DynamoDB returned them.

        Raises:
            KeyError: If `model` isn't in the collection
        """
        if model in self._by_model:
            return cast(list[M], self._by_model[cast(Any, model)])
        raise KeyError(f"{model.__name__} isn't in the collection")


class DynamoCollection[T: DynamoModel]:
    """Several models queried together, as they share a partition key.

    Every item a query returns is validated as the model whose primary key prefixes it
    matches, so no two models may have prefixes that the same key could start with.
    Each page's items can be read by model. E.g.

        user_models = DynamoCollection(User, Post)

        for page in user_models.query({"user_id": "u1"}):
            for user in page[User]:
                ...
            for post in page[Post]:
                ...

    Attributes:
        models: The models in the collection
        _prefixes: Each model's primary partition and sort key names, and the literal
            text every one of its keys starts with
    """

    def __init__(self, *models: type[T]):
        """Collect models, checking each item can only belong to one of them.

        Raises:
            ValueError: If no models are given, a model is given twice, the models'
                primary keys are stored under different attributes, or two models'
                key prefixes overlap
        """
        if not models:
            raise ValueError("A DynamoCollection needs at least one model")
        if len(set(models)) != len(models):
            raise ValueError("A DynamoCollection can't contain a model more than once")

        # The literal text before a key's first attribute; a column key has none
        self._prefixes = {
            model: tuple(
                (key.name, "" if key.is_column else key.format({}, partial=True))
                for key in (
                    model._meta.primary_index.partition,
                    model._meta.primary_index.sort,
                )
            )
            for model in models
        }

        # Items are matched by key prefix, so every model must store keys alike
        names = {
            tuple(name for name, _ in prefixes) for prefixes in self._prefixes.values()
        }
        if len(names) != 1:
            raise ValueError(
                "Every model in a DynamoCollection must store its primary key under the "
                "same attributes"
            )

        # An item whose keys could start with both models' prefixes is ambiguous
        for position, first in enumerate(models):
            for second in models[position + 1 :]:
                # Prefixes overlap when one starts the other, so a key could have both
                pairs = zip(self._prefixes[first], self._prefixes[second], strict=True)
                if all(a.startswith(b) or b.startswith(a) for (_, a), (_, b) in pairs):
                    raise ValueError(
                        f"{first.__name__} and {second.__name__} have overlapping "
                        "primary key prefixes, so their items can't be told apart"
                    )

        self.models = models

    def _resolve(self, item: Mapping[str, Any]) -> type[T]:
        """Get the model an item belongs to, by its primary key prefixes.

        Raises:
            ValueError: If the item matches none of the models
        """
        for model, prefixes in self._prefixes.items():
            if all(
                str(item.get(name, "")).startswith(prefix) for name, prefix in prefixes
            ):
                return model
        raise ValueError(
            f"The item {item} doesn't belong to any model in the collection"
        )

    def _table(self) -> Table:
        """Get the default table every model in the collection shares.

        Raises:
            ValueError: If a model has no default table, or the models' defaults differ
        """
        tables = [model.default_table() for model in self.models]
        if any(table is not tables[0] for table in tables[1:]):
            raise ValueError(
                "The models in the collection have different default tables: pass the "
                "table to query"
            )
        return tables[0]

    def _item_filter(self) -> ConditionBase | None:
        """Match the items of any model in the collection, or None if any item matches."""
        filters = []
        for prefixes in self._prefixes.values():
            conditions = [
                Attr(name).begins_with(prefix) for name, prefix in prefixes if prefix
            ]
            # A model without key prefixes matches every item, so nothing is filtered
            if not conditions:
                return None
            filters.append(functools.reduce(operator.and_, conditions))
        return functools.reduce(operator.or_, filters)

    def _partition_condition(
        self, values: Mapping[str, Any], index_name: str | None
    ) -> ConditionBase:
        """Get the partition key condition every model in the collection shares.

        Raises:
            ValueError: If a model doesn't have the index, a partition key value is
                missing or a comparator, or the models' partition keys differ
            TypeError: If a value isn't part of any model's partition key
        """
        # Build each model's partition key, noting the values it's built from
        partition_keys: set[tuple[str, Any]] = set()
        consumed: set[str] = set()
        for model in self.models:
            partition, _ = model._meta.get_keys(index_name)
            partition_values, _ = partition.extract_fields(values, required=True)
            if any(
                isinstance(value, Comparator) for value in partition_values.values()
            ):
                raise ValueError(
                    "DynamoCollection partition key values must be plain values, not "
                    "comparators"
                )
            partition_keys.add((partition.name, partition.format(partition_values)))
            consumed |= partition_values.keys()

        # One request can only read one partition, so every model must match the same
        if len(partition_keys) != 1:
            raise ValueError(
                "The models in the collection have different partition keys for "
                f"{dict(values)}, so they can't be queried together"
            )

        # Only partition key values are supported, as sort keys differ between models
        unknown = sorted(values.keys() - consumed)
        if unknown:
            raise TypeError(
                "DynamoCollection queries only take partition key values, not: "
                f"{', '.join(unknown)}"
            )

        ((name, value),) = partition_keys
        return Key(name).eq(value)

    def _pages(
        self, table: Table, request: dict[str, Any]
    ) -> Generator[CollectionPage[T]]:
        """Send the query until done, validating each item as the model it belongs to.

        Yields:
            A page per request, with valid items attached to `table` and checkpointed
        """
        while True:
            response = table.query(**request)

            # Load each item as its attached, checkpointed model, collecting failures
            items: list[T] = []
            errors: list[InvalidItem] = []
            for item in response.get("Items", []):
                try:
                    model = self._resolve(item).model_validate(item)
                except pydantic.ValidationError as error:
                    errors.append(InvalidItem(item=item, error=error))
                    continue

                model.attach(table)
                model.checkpoint()
                items.append(model)

            token = response.get("LastEvaluatedKey")
            yield CollectionPage(self.models, items, errors, token)

            # Stop when DynamoDB has no more results, otherwise continue after this page
            if token is None:
                break
            request["ExclusiveStartKey"] = token

    def query(
        self,
        values: Mapping[str, Any],
        index_name: str | None = None,
        *,
        table: Table | None = None,
        limit: int | None = None,
        scan_forward: bool = True,
        consistent_read: bool = False,
        start_token: dict | None = None,
    ) -> Generator[CollectionPage[T]]:
        """Query a partition, yielding a page per DynamoDB request.

        Values are only used to build the partition key, which must be the same for
        every model in the collection. Items of other models in the partition are
        filtered out, and items that fail validation are put in the page's `errors`.
        Each page's items are read by model, e.g. `page[Post]`.

        Args:
            values: The partition key attribute values, by attribute name
            index_name: A secondary index every model has, or None for the primary index
            table: The table to query, or None for the models' shared default table;
                returned models are attached to it
            limit: DynamoDB's `Limit`, applied to each request
            scan_forward: Return items in ascending sort key order
            consistent_read: Use strongly consistent reads
            start_token: A page's token to resume the query from

        Raises:
            ValueError: If there is no table, `limit` isn't positive, a model doesn't
                have the index, a partition key value is missing or a comparator, or
                the models' partition keys differ
            TypeError: If a value isn't part of any model's partition key
        """
        table = table or self._table()
        key_condition = self._partition_condition(values, index_name)

        # The request options are the same as a single model's query
        request = self.models[0]._request(
            {}, index_name, limit, consistent_read, start_token
        )
        request["ScanIndexForward"] = scan_forward
        request["KeyConditionExpression"] = key_condition
        item_filter = self._item_filter()
        if item_filter is not None:
            request["FilterExpression"] = item_filter

        yield from self._pages(table, request)
