# Considerations

These are the limitations and quirks of the implementation, to be worked into the
package documentation.

## Models (`tidal.models`)

### The default table is inherited by class attribute lookup

`DynamoModel.default_table(table)` stores the table on the class it's called on.
Subclasses find it through normal class attribute lookup, so the nearest class that
set a default wins. It's one table per class for the whole process. If the table must
differ per request, pass it to each call or set the default at the start of each
request.

### `update` doesn't run model validators

`update` validates each value against a copy of the model that has only its fields and
field validators. Model validators (`@model_validator`) aren't run, because they need
the whole model and `update` has only the changed values. Field validators that read
other fields through `ValidationInfo.data` see no other fields. The item DynamoDB
returns is validated in full, but only after it has been written.

### `update` replaces lists, dicts and union-typed models whole

A mapping is a partial update only for a field whose type is a single pydantic model,
optionally `| None`. Lists, sets, dicts and fields typed as a union of several models
are replaced whole. To change part of a list, `get` the item, change it, and `put` it.
`put` diffs against the checkpoint and sends only the attributes that changed.

### Partial `update`s need the parent map to exist

`identity={"username": "new"}` sets `identity.username` in DynamoDB. If the stored
`identity` is missing or `null`, DynamoDB rejects the document path, and `update`
raises `ValueError`. Set the whole attribute with a model instance first.

### `update` can't rewrite index keys from partial or computed values

A secondary index key is rewritten whole, so every attribute in its template needs a
plain value: either one passed to `update` or a primary key value. An update that
touches a key attribute raises `ValueError` before anything is sent in these cases:

- another attribute of that key isn't given;
- the attribute is given a nested mapping or an `increment`;
- the key is built from a property.

Use `put` for these changes.

### `increment` isn't validated against the field

`increment` checks that the amount is an `int` or `Decimal`, but DynamoDB computes the
result. The field's type and validators, such as `ge=0`, are only checked when the
returned item is validated, after the write.

## GraphQL API (`tidal.api`)

### Only some Python types map to GraphQL

Unions of more than one type besides `None`, `Literal`, `tuple`, and generics other than
`list`, `set`, `frozenset` and `dict` raise `SchemaError`. GraphQL unions and interfaces
aren't generated. `Decimal` is published as `Float`, so clients see a double, which can
lose precision.

### Model properties are fields only when annotated

A model's public properties are output fields if they have a return annotation.
Unannotated properties, names starting with `_` and `functools.cached_property` are
left out. Properties can't take GraphQL arguments, so a field like `posts(limit: Int)`
isn't possible. Input types only have the model's fields.

### `Page` publishes only `items` and `token`

`Page[User]` is published as `UserPage`. Its `errors` aren't published, because they
hold raw items and pydantic errors. Other generic models are named in the same way,
with their arguments followed by their own name, and their arguments must be classes.

### Argument defaults aren't in the schema

An argument whose parameter has a default is optional in the schema, even if its type
isn't nullable, and the default isn't shown. Clients can't see what value an omitted
argument takes.

An omitted argument takes its parameter's default. Without a default, it's `None` if
the annotation accepts `None`, and an `ArgumentError` otherwise. An explicit `null`
is passed as `None` when the annotation accepts it, and otherwise takes the default.
So for `limit: int = 10`, `null` and omitting `limit` both give `10`.

### Arguments are validated by Python field name

Input objects are renamed to Python field names and validated with
`by_alias=False, by_name=True`. Field aliases, such as a `DynamoModel`'s stored
attribute names, play no part in the GraphQL API. Validation runs with pydantic's
defaults, so lax conversions apply, e.g. `"1"` is accepted for an `int`.

### Results must be the annotated models

A resolver annotated `-> User` must return a `User` (or a subclass), not a dict. A
subclass's extra fields aren't published, because the schema follows the annotation.

### Only selected fields are read

A result is encoded from AppSync's `selectionSetList`, so properties the request
didn't select never run. Every selected property does run, once per object, so a
property that queries DynamoDB in a list of `n` items makes `n` queries.

### Numbers and dates are converted for JSON

`Decimal` fields are returned as floats. Inside `dict` (`AWSJSON`) values, `Decimal`s
are returned as integers when whole and floats otherwise, as DynamoDB returns every
number as a `Decimal`. Dates and times are returned with `isoformat()`. AppSync requires
an offset on `AWSDateTime`, so naive `datetime`s are rejected; use aware datetimes.
`AWSJSON` arguments are parsed if they arrive as JSON text.

### Only field and argument names are converted

The router's name converter renames fields, arguments and resolvers. Type names stay
the class name, and enum values stay the member names, not the members' values.

### Only `Query` and `Mutation` fields are resolved

The router resolves events whose `parentTypeName` is `Query` or `Mutation`. Fields of
other types are resolved from the returned object, so attach the Lambda resolver to
root fields only. Subscriptions and AppSync's batch invoke (`BatchInvoke`) aren't
supported.

### Resolvers must be synchronous functions

Resolvers must be plain functions; `async def` functions raise `SchemaError` when
registered. Exceptions they raise are not caught. AppSync reports them to the client
with the exception's class name as the `errorType`, so those class names are part of
the API.

### Resolver mistakes surface when the schema is built

Annotations are resolved when first used, so they can reference classes defined later.
An unsupported annotation is only reported when the schema is built, or when the
resolver is first called. Call `router.sdl()` in a test or deploy step to catch these
before requests do.

### `include_router` copies resolvers once

`include_router` copies the resolvers registered on the other router at the time it's
called, so later registrations on that router aren't included. Both routers must use
the same name converter object, since resolver names are converted when registered.

### Generic models may be returned unparametrised

A resolver annotated `-> Page[User]` may return a plain `Page`, as `DynamoModel.query`
does. The fields published, and the annotations used to encode them, come from the
return annotation, not from the returned object.

## Streams (`tidal.streams`)

### Changes are matched to models by validation

A change is given to the first model registered for its event that validates its image. Nothing checks the item's key, so a model can accept another item type's changes when its fields are a subset of the other's, or when lax conversions let the other's values through. Register the more specific model first, and give item types a required field the others lack. Generated key attributes such as `PK` and `SK` can't tell models apart, because pydantic ignores attributes a model doesn't declare.

### Images aren't attached to a table

The images passed to handlers are validated with `model_validate`, so they aren't attached or checkpointed like models read with `get`. Calling `put` on one writes the whole item to the default table. To change the item, `update` it, or `get` it and then `put` it.

### A failing handler's change is skipped by default

When a handler raises, the exception is logged and the batch carries on, so that change is never retried. Pass `raise_on_error=True` to fail the invocation instead, which makes Lambda retry the batch.

### An unmatched change blocks its shard

`HandlerNotFoundError` fails the whole invocation, and Lambda retries the batch until the record succeeds or expires from the stream. Records behind it on the same shard wait until then. Register a handler for every item type and event the table produces, or limit retries on the event source mapping with `MaximumRetryAttempts` and `BisectBatchOnFunctionError`.

### Retries repeat changes that were already handled

A failed invocation is retried from the start of the batch, and a timeout from the unfinished record. Handlers that already ran for earlier records in the batch run again. AWS also documents that Kinesis Data Streams for DynamoDB can deliver a change more than once, and out of order. Handlers should be idempotent, and can compare `ApproximateCreationDateTime` or the item's own version if order matters.

### Only Kinesis delivery is supported

Records must come from a Kinesis streaming destination. Lambda events from DynamoDB Streams have a different shape and aren't supported. Each change must carry its item images, otherwise it raises `HandlerNotFoundError`.

### The timeout uses `SIGALRM`

The timeout is scheduled with `signal.alarm`, which replaces any other `SIGALRM` handler while the batch runs. Where `SIGALRM` doesn't exist, such as on Windows, there's no timeout handling. Lambda runs on Linux, so this only affects local runs.

### Handlers are synchronous and their results are ignored

Handlers are plain functions called one record at a time, in batch order. `async def` handlers aren't awaited, and return values are discarded.
