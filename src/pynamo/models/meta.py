"""Storage settings read from the `Meta` class every `DynamoModel` subclass defines.

Users declare a plain nested class, with nothing to import or inherit, e.g.

    class Order(DynamoModel):
        customer_id: str
        order_id: str

        class Meta:
            indexes = single_table_indexes(
                base=("CUSTOMER#{customer_id}", "ORDER#{order_id}"),
            )
            table = orders_table  # Optional

`DynamoModel` passes that class to `DynamoMeta`, which checks it against the model
once and holds the settings in the form requests need, as the model's `_meta`. The
declared `Meta` is left as it is.
"""

import pydantic
from mypy_boto3_dynamodb.service_resource import Table

from .indexes import GlobalSecondaryIndex, KeyPair, LocalIndex, PrimaryIndex

type SecondaryIndex = LocalIndex | GlobalSecondaryIndex

# The settings a Meta class may declare, so a misspelt one isn't silently ignored
_SETTINGS = frozenset({"indexes", "table"})


class DynamoMeta:
    """A model's storage settings, read from its `Meta` class and checked once.

    Attributes:
        indexes: The model's index definitions, with exactly one `PrimaryIndex`
        table: The table used when a method isn't given one, if any
        primary_index: The primary index, taken from `indexes`
        secondary_indexes: The local and global indexes in `indexes`, by name
        generated_keys: The template keys of every index, written alongside the fields
    """

    def _split_indexes(self) -> tuple[PrimaryIndex, dict[str, SecondaryIndex]]:
        """Separate `indexes` into the primary index and the secondary indexes by name.

        Raises:
            TypeError: If an index isn't a supported type
            ValueError: If there isn't exactly one `PrimaryIndex`, or names repeat
        """
        primaries: list[PrimaryIndex] = []
        secondaries: dict[str, SecondaryIndex] = {}
        for index in self.indexes:
            # Secondary indexes are looked up by name, so names must be unique
            if isinstance(index, (LocalIndex, GlobalSecondaryIndex)):
                if index.name in secondaries:
                    raise ValueError(
                        f"{self._label}.indexes repeats the name {index.name!r}"
                    )
                secondaries[index.name] = index
            elif isinstance(index, PrimaryIndex):
                primaries.append(index)
            else:
                raise TypeError(f"Unsupported index type: {type(index).__name__}")

        if len(primaries) == 1:
            return primaries[0], secondaries
        raise ValueError(f"{self._label}.indexes must contain exactly one PrimaryIndex")

    def _generated_keys(self) -> tuple[KeyPair, ...]:
        """Get the template keys of every index, which are written alongside the fields.

        Column keys are fields, so the model dump already writes them.
        """
        keys: list[KeyPair] = []
        for index in self.indexes:
            # A local index only defines a sort key; its partition key is the primary's
            if isinstance(index, LocalIndex):
                keys.append(index.sort)
            else:
                keys.extend((index.partition, index.sort))
        return tuple(key for key in keys if not key.is_column)

    def __init__(self, meta: type, model: type[pydantic.BaseModel]):
        """Read `meta`'s settings and check them against `model`.

        Raises:
            TypeError: If `meta` declares an unknown setting, doesn't declare its own
                `indexes` as a list, or holds an unsupported index type
            ValueError: If there isn't exactly one `PrimaryIndex`, secondary index names
                repeat, or a key references an attribute the model doesn't define
        """
        self._label = f"{model.__name__}.Meta"

        # Public names on the Meta must be known settings, catching typos like `tabel`
        unknown = sorted(
            name
            for name in vars(meta)
            if not name.startswith("_") and name not in _SETTINGS
        )
        if unknown:
            raise TypeError(f"{self._label} has unknown settings: {', '.join(unknown)}")

        # Indexes must be declared on this Meta itself, so a model never reuses another's keys
        indexes = vars(meta).get("indexes")
        if not isinstance(indexes, list):
            raise TypeError(f"{self._label} must declare its own indexes as a list")

        self.indexes: list[PrimaryIndex | SecondaryIndex] = indexes
        self.table: Table | None = getattr(meta, "table", None)
        self.primary_index, self.secondary_indexes = self._split_indexes()
        self.generated_keys = self._generated_keys()

        # Every key template may only reference the model's attributes
        for index in indexes:
            index.validate(model)

    def get_index(self, name: str | None) -> PrimaryIndex | SecondaryIndex:
        """Get the primary index for None, else the secondary index named `name`.

        Raises:
            ValueError: If there is no secondary index named `name`
        """
        if name is None:
            return self.primary_index
        index = self.secondary_indexes.get(name)
        if index is not None:
            return index
        raise ValueError(f"{self._label} has no index named {name!r}")

    def get_keys(self, name: str | None) -> tuple[KeyPair, KeyPair]:
        """Get the partition and sort key of the index `get_index` finds for `name`.

        Raises:
            ValueError: If there is no secondary index named `name`
        """
        index = self.get_index(name)

        # A local index only defines a sort key, sharing the primary partition key
        if isinstance(index, LocalIndex):
            return self.primary_index.partition, index.sort
        return index.partition, index.sort
