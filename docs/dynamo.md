# Dynamo models

`pynamo.dynamo.DynamoModel` is a pydantic base class for items stored in a DynamoDB
table. A model declares how its keys are built from its attributes, and can then be
read with `get`, `query` and `scan`, and changed with `put`, `update`, `delete` and
`remove`.

## Defining a model

Set `indexes` to the table's index definitions. Keys are `str.format` templates over
the model's attributes. `single_table_indexes` names them using single-table
conventions: `PK`/`SK`, `LSI<n>SK`, and `GSI<n>PK`/`GSI<n>SK`.

```python
from pynamo.dynamo import DynamoModel, single_table_indexes


class Order(DynamoModel):
    indexes = single_table_indexes(
        base=("CUSTOMER#{customer_id}", "ORDER#{date}#{order_id}"),
        local=["ORDER_BY_STATUS#{status}#{date}"],
        global_indexes=[("ORDER#{order_id}", "ORDER")],
    )

    customer_id: str
    order_id: str
    date: str
    status: str
    total: int
```

These rules are checked when the class is defined:

- `indexes` must contain exactly one `PrimaryIndex`, and index names must be unique.
- Template fields must name top-level attributes of the model, such as fields or
  properties. Nested fields like `{owner.id}` are rejected, so expose a nested value as
  a top-level property instead. Format specs like `{n:010d}` are allowed.

## Choosing the table

Every method takes a table, which is optional:

- `get`, `update` and `remove` take it as the first positional argument, so a key
  attribute can also be called `table`.
- `query` and `scan` take it as the keyword `table=`.
- `put` and `delete` default to the table the model is attached to.

When no table is given, the default table set with `default_table` is used. A default
applies to the class and its subclasses, unless a subclass sets its own, so setting it
on `DynamoModel` covers every model:

```python
table = boto3.resource("dynamodb").Table("app")
DynamoModel.default_table(table)

Order.get(customer_id="c1", date="2024-01-01", order_id="o1")  # Reads from `table`
```

The default table is separate from the table an instance is attached to. An instance
uses a table it's given first, then its attached table, then the default. Writing an
unattached model to the default table doesn't attach it.

## Reading items

### `get`

`Order.get(table, customer_id="c1", date="2024-01-01", order_id="o1")` builds the
primary key from the values and returns the item. The model is attached to the table
and checkpointed.

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
different table, or detaching it, drops its checkpoint.

A model
whose primary key has changed since its checkpoint can't be updated, because DynamoDB
can't change an item's key, so `put` raises `ValueError`. Detach it first to write it as
a new item; the old item is left in place.

```python
order = Order.get(table, customer_id="c1", date="2024-01-01", order_id="o1")
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
the primary key attributes and the attributes to change:

```python
from pynamo.dynamo import increment

Account.update(account_id="a1", plan="pro")
Account.update(account_id="a1", identity={"username": "new"})
Account.update(account_id="a1", likes=increment(1))
```

- **Values are validated before anything is sent.** Each one is checked with its field's
  type and field validators. Unknown fields raise `TypeError`, and invalid values raise
  `pydantic.ValidationError`.
- **A mapping for a field holding a model updates only the keys it names.** This works at
  any depth, and the other keys in the stored map are left alone. Pass `None` to clear a
  key. Pass a model instance to replace the whole attribute.
- **Any other value replaces the attribute**, including lists and dicts.
- **`increment(n)` adds `n` to a number in DynamoDB**, starting from 0 if the attribute
  is missing. Use a negative `n` to decrement.
- **Index keys are rewritten.** Secondary index keys built from an updated attribute are
  regenerated, which needs a plain value for every attribute in that key.
- **The item must already exist**, otherwise `update` raises `KeyError`.

It returns the updated item, attached and checkpointed. See
[CONSIDERATIONS.md](CONSIDERATIONS.md) for its limitations.

### `remove`

`Order.remove(table, customer_id="c1", date="2024-01-01", order_id="o1")` deletes an
item without needing its model. Like `get`, it builds the primary key from the keyword
arguments, and a missing key attribute raises `KeyError`.

### `query`

`query` builds the key condition from attribute values, and turns any remaining values
into filters. It yields one `Page` per DynamoDB request:

```python
from pynamo.dynamo import between

for page in Order.query({"customer_id": "c1", "date": between("2024-01", "2024-03")}):
    for order in page.items:
        ...
    for invalid in page.errors:
        ...
```

- Every attribute in the index's partition key template must have a plain value.
  Comparators are not allowed there.
- Sort key values fix the key in template order. The first missing attribute, or the
  first attribute given a comparator, ends the key condition, and later values become
  filters.
- Pass `index_name` to query a secondary index.

### `scan`

`scan` takes the same `values` as `query`, but every value becomes a filter. `values`
is optional.

```python
for page in Order.scan({"status": "open"}):
    ...
```

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
