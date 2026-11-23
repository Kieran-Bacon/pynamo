# Models

`pynamo.models` is where data in a pynamo application is defined and stored. `DynamoModel` is a pydantic base class for items stored in a DynamoDB table. A model declares how its keys are built from its attributes, and can then be read with `get`, `query` and `scan`, and changed with `put`, `update`, `delete` and `remove`.

The same classes are used by the rest of pynamo. [`pynamo.api`](api.md) publishes them as GraphQL types, and [`pynamo.streams`](streams.md) validates the item before and after each change as them. Every write made here, through `put`, `update`, `delete` or `remove`, is a change that stream handlers receive.

## Defining a model

Every model declares a nested `class Meta`, with nothing to import or inherit. Its settings are:

- `indexes` (required): the table's index definitions, as a list. Keys are `str.format` templates over the model's attributes.
- `table` (optional): the model's default table, see [Choosing the table](#choosing-the-table).

`single_table_indexes` builds the index list using single-table naming conventions: `PK`/`SK`, `LSI<n>SK`, and `GSI<n>PK`/`GSI<n>SK`, numbered in the order given.

```python
from pynamo.models import DynamoModel, single_table_indexes


class Order(DynamoModel):
    customer_id: str
    order_id: str
    date: str
    status: str
    total: int

    class Meta:
        indexes = single_table_indexes(
            base=("CUSTOMER#{customer_id}", "ORDER#{date}#{order_id}"),
            local=["ORDER_BY_STATUS#{status}#{date}"],
            global_indexes=[("ORDER#{order_id}", "ORDER")],
        )
```

Indexes can also be declared directly, to name the keys yourself. A `KeyPair` is a key attribute's name and its template:

```python
from pynamo.models import GlobalSecondaryIndex, KeyPair, LocalIndex, PrimaryIndex


class Order(DynamoModel):
    ...

    class Meta:
        indexes = [
            PrimaryIndex(KeyPair("pk", "CUSTOMER#{customer_id}"), KeyPair("sk", "ORDER#{date}#{order_id}")),
            LocalIndex("by_status", KeyPair("status_sk", "ORDER_BY_STATUS#{status}#{date}")),
            GlobalSecondaryIndex("by_order", KeyPair("order_pk", "ORDER#{order_id}"), KeyPair("order_sk", "ORDER")),
        ]
```

A `KeyPair` without a template, such as `KeyPair("order_id")`, is a column key: the model's own field of that name, stored as it is rather than generated. In `single_table_indexes`, a `None` template does the same, e.g. `base=(None, "ORDER")` makes `PK` the model's `PK` field. See [CONSIDERATIONS.md](CONSIDERATIONS.md) for how column keys differ.

These rules are checked when the class is defined:

- Every model declares its own `Meta`, including subclasses of another model, so a model never silently reuses another's keys. A model without one raises `TypeError`.
- `Meta` may only set `indexes` and `table`, so a misspelt setting raises `TypeError`. `indexes` must be a list.
- `indexes` must contain exactly one `PrimaryIndex`, and secondary index names must be unique.
- Template fields must name top-level attributes of the model, such as fields or properties. Nested fields like `{owner.id}` are rejected, so expose a nested value as a top-level property instead. Format specs like `{n:010d}` are allowed.
- A column key must be a field stored under its own name, not under a different alias.

## Choosing the table

Every method takes a table, which is optional:

- `get`, `remove`, `update`, `query` and `scan` take it as the keyword `table=`.
- `put` and `delete` take it as their first argument, and default to the table the model is attached to.

When no table is given, the model's default table is used. `Meta.table` sets a model's default when it's defined, and `default_table(table)` sets it later. A default applies to the class and its subclasses, unless a subclass sets its own, so setting it on `DynamoModel` covers every model without a `Meta.table`:

```python
table = boto3.resource("dynamodb").Table("app")
DynamoModel.default_table(table)

Order.get({"customer_id": "c1", "date": "2024-01-01", "order_id": "o1"})  # From `table`
```

Calling `default_table()` with no table returns the default, and raises `ValueError` if there isn't one.

The default table is separate from the table an instance is attached to. An instance uses a table it's given first, then its attached table, then the default. Writing an unattached model to the default table doesn't attach it.

## Reading items

### `get`

`Order.get({"customer_id": "c1", "date": "2024-01-01", "order_id": "o1"}, table=table)`
builds the primary key from the `key` mapping and returns the item. The mapping must hold
exactly the attributes the primary key is built from: a missing one raises `ValueError`
(`KeyError` for a column key), and so does one the key doesn't use. The model is
attached to the table and checkpointed.

For a model with a single item, whose key templates reference no attributes (e.g.
`base=("SETTINGS", "SETTINGS")`), the key can be left out: `Settings.get()`.

### `put`

`order.put(table)` writes the model, the inverse of `get`. The stored item is the
model's `model_dump(by_alias=True)` plus every index's key attributes, generated from
the templates. The table defaults to the one the model is attached to, then the default
table.

Models are attached and checkpointed when they're read with `get`, `query` or `scan`.
The checkpoint records the item as stored in the attached table, and it decides how
`put` writes:

- **With a checkpoint for the table:** `put` sends an `update_item` that sets only the
  attributes that changed, including any index keys built from them, and removes any
  that are gone. If nothing changed, nothing is sent. The update requires the item to
  still exist, so a deleted item isn't recreated with only the changed attributes.
- **Otherwise:** `put` sends a `put_item` of the whole item, replacing any existing item
  with the same primary key.

After writing to the attached table, `put` checkpoints the model, as that table now
holds its state. Passing a different table is a one-off put: the model isn't attached
to it, and its attachment and checkpoint are left as they are. Attaching a model to a
different table, or detaching it, drops its checkpoint. Pass `allow_update=False` to always send a `put_item` of the whole item, even with a checkpoint.

A model whose primary key has changed since its checkpoint can't be updated, because DynamoDB can't change an item's key, so `put` raises `ValueError`. Detach it first to write it as a new item; the old item is left in place.

```python
order = Order.get({"customer_id": "c1", "date": "2024-01-01", "order_id": "o1"})
order.status = "shipped"
order.put()  # Updates status and the LSI1SK key built from it
```

### `delete`

`order.delete(table)` deletes the model's item, using the table the model is attached
to by default. If the model has a checkpoint for the table, the item is found by the
checkpointed primary key, so it's still the stored item that's deleted after the key
attributes change. Otherwise the key is generated from the model. Deleting from the
attached table drops the checkpoint, so a later `put` writes the whole item; the model
stays attached. Deleting from a different table leaves the checkpoint as it is.

### `update`

`update` changes an existing item by its primary key, without needing its model. Pass
the primary key's attributes, as for `get`, and then the changes:

```python
from pynamo.models.updates import Increment

Account.update({"account_id": "a1"}, {"plan": "pro"})
Account.update({"account_id": "a1"}, {"identity": {"username": "new"}})
Account.update({"account_id": "a1"}, {"likes": Increment(1)})
```

- **`changes` can't be empty**, otherwise `update` raises `ValueError`. For a model whose key templates reference no attributes, leave the key out and pass the changes by name: `Settings.update(changes={"theme": "dark"})`.
- **The primary key can't be changed.** A change to an attribute the primary key is
  built from raises `ValueError`; delete the item and put it under its new key instead.
- **Values are validated before anything is sent.** Each one is checked with its field's
  type and field validators. Unknown fields raise `TypeError`, and invalid values raise
  `pydantic.ValidationError`.
- **A mapping for a field holding a model updates only the keys it names.** This works at
  any depth, and the other keys in the stored map are left alone. Pass `None` to clear a
  key. Pass a model instance to replace the whole attribute.
- **Any other value replaces the attribute**, including lists and dicts.
- **`Increment(n)` adds `n` to a number in DynamoDB**, starting from 0 if the attribute
  is missing. Use a negative `n` to decrement.
- **Index keys are rewritten.** Secondary index keys built from an updated attribute are
  regenerated, which needs a plain value for every attribute in that key.
- **The item must already exist**, otherwise `update` raises `KeyError`.

It returns the updated item, attached and checkpointed. See
[CONSIDERATIONS.md](CONSIDERATIONS.md) for its limitations.

### `remove`

`Order.remove({"customer_id": "c1", "date": "2024-01-01", "order_id": "o1"})` deletes an
item without needing its model. It takes the same `key` mapping as `get`, which can
likewise be left out for a single-item model: `Settings.remove()`.

### `query`

`query` builds the key condition from attribute values, and turns any remaining values into filters. A value is either a plain value, which matches by equality, or a comparator from `pynamo.models.conditions`. It yields one `Page` per DynamoDB request:

```python
from pynamo.models.conditions import Between

for page in Order.query({"customer_id": "c1", "date": Between("2024-01", "2024-03")}):
    for order in page.items:
        ...
    for invalid in page.errors:
        ...
```

The key condition is built like this:

- **Partition key:** every attribute in the index's partition key template needs a plain value. A missing one, or a comparator, raises `ValueError`.
- **Sort key:** values are taken in template order, up to the first attribute without one.
  - A plain value for every attribute matches the whole key exactly, e.g. `SK = 'ORDER#2024-01-01#o1'`.
  - Plain values for only the first attributes match the keys that begin with them, e.g. `begins_with(SK, 'ORDER#2024-01-01#')`. With no values, the key matches on the text before its first attribute, `begins_with(SK, 'ORDER#')`, so other item types in the partition aren't read.
  - The first comparator ends the key condition. If it can narrow a key, it's applied to the key text, together with the plain values before it. If it's filter-only, it becomes a filter.
- **Everything else is a filter:** sort key values after the key condition ends, and every attribute that isn't part of the key. A name the model doesn't have raises `TypeError`.

The comparators are:

| Comparator | Matches | On the sort key |
| --- | --- | --- |
| `Equal(value)` | equal to `value`, as a plain value does | key condition |
| `Between(low, high)` | from `low` to `high`, including both | key condition |
| `BeginsWith(prefix)` | text starting with `prefix` | key condition |
| `GreaterThan(value)`, `GreaterThanOrEqual(value)` | above `value` | key condition |
| `LessThan(value)`, `LessThanOrEqual(value)` | below `value` | key condition |
| `NotEqual(value)` | not equal to `value` | filter |
| `In(values)` | one of a tuple of `values` | filter |
| `Contains(value)` | a string containing, or a set or list holding, `value` | filter |
| `Exists()`, `NotExists()` | the attribute is present, or missing | filter |

On the sort key, a comparator compares key text, not attribute values, so `9` sorts after `10`. A comparator on an attribute in the middle of a key, like `date` in `ORDER#{date}#{order_id}`, matches every key with a matching value, whatever follows it. See [CONSIDERATIONS.md](CONSIDERATIONS.md) for how ranges are built.

`query` also takes:

- `index_name`: a secondary index to query. A local index uses the table's partition key.
- `table`, `limit`, `consistent_read` and `start_token`, see [Pages](#pages).
- `scan_forward`: False returns items in descending sort key order.

### `scan`

`scan` takes the same `values` as `query`, but every value becomes a filter, and `values` is optional. Items are also filtered to the model's own by the text its primary keys start with (see [Considerations](#scans-match-item-types-by-key-prefix)). It takes the same arguments as `query`, except `scan_forward`; `index_name` scans a secondary index.

```python
for page in Order.scan({"status": "open"}):
    ...
```

### Querying several models at once

In a single-table design, different models often share a partition, such as a user's profile and their posts under `USER#{user_id}`. A `DynamoCollection` queries that partition once and returns each item as the model it belongs to:

```python
from pynamo.models.collection import DynamoCollection

user_models = DynamoCollection(User, Post)

for page in user_models.query({"user_id": "u1"}):
    for user in page[User]:
        ...
    for post in page[Post]:
        ...
```

Each page is a `CollectionPage`. Indexing it with a model gives that model's items as a list, typed as that model, in the order DynamoDB returned them. Every model in the collection has a list, which is empty when the page has none of its items, and a model that isn't in the collection raises `KeyError`. The page also has `items`, every model in the order returned, and the same `errors` and `token` as a model's pages.

`query` takes the same `index_name`, `table`, `limit`, `scan_forward`, `consistent_read` and `start_token` arguments as `DynamoModel.query`. Without a `table`, every model must have the same default table. For now, `values` can only hold the partition key's attributes, and every model must build the same partition key from them: the same key attribute and the same key text. Sort key conditions and filters differ between models, so they aren't supported yet.

Each item is matched to a model by the literal text its primary keys start with, e.g. `PROFILE` for `User` and `POST#` for `Post`. The query filters out items of models that aren't in the collection. The collection raises when it's created if two models' prefixes overlap, so an item could belong to either, or if the models store their primary keys under different attributes.

### Pages

Each page has:

- `items`: the valid models, attached to the table and checkpointed.
- `errors`: an `InvalidItem` for each item that failed validation, holding the raw
  `item` and the pydantic `error`. Failures are collected here rather than raised.
- `token`: DynamoDB's `LastEvaluatedKey`, which is None on the last page. Pass it as
  `start_token` to resume later.

`limit` is passed to DynamoDB as `Limit` on each request. DynamoDB applies it before
filtering, so a page can hold fewer items than `limit`.

## Considerations

### Scans match item types by key prefix

A single-table scan reads every item type. `scan` narrows the results to the model's
items by filtering on the literal text at the start of each primary key template. For
`Order`, that filter is:

```
begins_with(PK, 'CUSTOMER#') AND begins_with(SK, 'ORDER#')
```

This has three limitations:

- **Shared prefixes still match.** Another item type's key that starts with the same
  text, such as `ORDER#ARCHIVE#{id}` next to `ORDER#{date}#{order_id}`, passes the
  filter. Those items then fail validation and appear in `page.errors`, or, if they
  happen to validate, in `page.items`. Give each item type a distinct key prefix to
  avoid this.
- **Templates that start with a field aren't filtered.** A template like `"{source}"`
  has no leading text, so that key adds no filter.
- **Filters don't reduce reads.** DynamoDB applies filters after reading, so a scan
  still reads, and is billed for, the whole table or index. Pages can be small or
  empty while `token` is still set.

The filter is on the table's primary key attributes, which DynamoDB copies into every
secondary index, so the same filtering applies when you scan an index.

### `put` writes attributes as pydantic dumps them

`put` sends `model_dump(by_alias=True)` to boto3 unchanged. Attribute types that boto3
can't serialise, such as `float` (use `Decimal`) or `datetime`, make the write fail.
Key templates format values with `str.format`, so a `None` attribute becomes the text
`"None"` in its key rather than leaving that index's key out.

### Query and scan arguments are checked lazily

`query` and `scan` are generators, so invalid arguments raise when the first page is
requested, not when the method is called. Examples are a missing table, an unknown
attribute, an unknown index, a non-positive `limit`, or a missing partition key value.
