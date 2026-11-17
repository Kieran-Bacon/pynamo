# Streams

`tidal.streams` runs your code after items change. DynamoDB publishes every change to an item, whether it's an insert, a modification or a removal, to a Kinesis data stream. Lambda reads that stream in batches, and `DynamoStreamHandler` validates the item before and after each change as one of your pydantic models and calls the function registered for it.

This is the event-driven half of a tidal application. A [resolver](api.md) or any other writer only has to [store a model](models.md); the follow-on work, such as sending notifications, keeping counts, copying data to a search index or auditing changes, happens here. The follow-on work runs after the write has succeeded, and no writer needs to know about it.

## Setting up the stream

`DynamoStreamHandler` reads DynamoDB changes delivered through Kinesis Data Streams. It needs:

1. **A Kinesis streaming destination on the table.** This publishes each item change to a Kinesis data stream, including the item before (`OldImage`) and after (`NewImage`) the change.
2. **A Lambda function with an event source mapping on that Kinesis stream.** Turn on `ReportBatchItemFailures` in the mapping's function response types, so Lambda retries from the record the handler reports rather than repeating the whole batch.

## Registering handlers

Create a `DynamoStreamHandler` and decorate functions with `handle`, naming the model whose changes they handle:

```python
from tidal.streams import DynamoStreamHandler

stream = DynamoStreamHandler()


@stream.handle(Order, events=["INSERT"])
def on_order_placed(old: Order | None, new: Order | None) -> None:
    """Email the customer a confirmation of their new order."""
    ...


@stream.handle(Order, events=["MODIFY"])
def on_order_updated(old: Order | None, new: Order | None) -> None:
    """Tell the customer when their order ships."""
    if old.status != "shipped" and new.status == "shipped":
        ...
```

Every handler is called with the item's old and new images, validated as the model:

| Event | `old` | `new` |
| --- | --- | --- |
| `INSERT`: the item was created | `None` | the item |
| `MODIFY`: the item was changed | the item before | the item after |
| `REMOVE`: the item was deleted | the item | `None` |

`events` defaults to all three, so a handler can check which images are present to tell the changes apart. `handle` returns the function unchanged, so it can still be called directly in tests.

The model can be any pydantic model. A `DynamoModel` is the natural choice, since the images are the stored items. The images also hold the generated key attributes, such as `PK` and `SK`, and pydantic ignores those because the model doesn't declare them.

## How a change finds its handler

A single table holds many item types, so each change has to be matched to a model. For each record, the handler:

1. Decodes the DynamoDB change from the Kinesis record and converts its images from DynamoDB's typed JSON to Python values.
2. Validates the old image, or the new image for an `INSERT`, against each model registered for the event, in registration order. The first model that validates is chosen.
3. Validates the other image as the same model, and calls the function registered for that model and event.

Models are matched by validation, not by key, so a model whose fields are a subset of another's can accept the other's items. Register the more specific model first. See [Considerations](CONSIDERATIONS.md) for more on this.

## Running in Lambda

Use the handler's `lambda_handler` as the function's entry point:

```python
def lambda_handler(event, context):
    return stream.lambda_handler(event, context)
```

It processes the batch's records in order and returns a partial batch response for Lambda.

## Combining handlers

Handlers can be split across modules, one per area of the application, and merged with `add`:

```python
stream = DynamoStreamHandler()
stream.add(orders.stream)
stream.add(users.stream)
```

A registration from the added handler replaces any existing one for the same model and event.

## Failures and retries

| What happens | Result |
| --- | --- |
| A handler raises | The exception and the change's images are logged, and processing moves on to the next record. The failed change isn't retried. |
| A handler raises, with `raise_on_error=True` | `UnhandledUserCodeException` is raised from the handler's exception. The invocation fails, and Lambda retries the whole batch. |
| No registered model accepts a change | `HandlerNotFoundError` is raised. The invocation fails, and Lambda retries the whole batch. |
| The invocation is about to time out | One second before the deadline, processing stops. The unfinished record is reported as a batch item failure, so Lambda retries from it. |

Pass `raise_on_error` to `lambda_handler` to stop on the first failing handler:

```python
def lambda_handler(event, context):
    return stream.lambda_handler(event, context, raise_on_error=True)
```

Retries run handlers again for changes they've already handled, so handlers should be idempotent. See [Considerations](CONSIDERATIONS.md) for how retries, ordering and unmatched changes behave.

## Errors

- `HandlerNotFoundError`: a change has no handler for its event, no registered model accepts it, or it carries no item images.
- `UnhandledUserCodeException`: a handler raised while `raise_on_error` was set. The handler's exception is its `__cause__`.
- `LambdaTimeoutException`: raised internally when the invocation is about to time out, and caught by `lambda_handler`, which reports the unfinished record for retry.
