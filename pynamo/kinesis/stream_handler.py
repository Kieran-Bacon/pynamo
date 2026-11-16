"""Dispatch DynamoDB item changes, delivered to Lambda through Kinesis, to typed handlers.

Handlers are registered against a pydantic model and the change events they care about.
Each record's old and new images are validated as the first registered model that
accepts them and passed to that model's handler:

    stream = DynamoStreamHandler()

    @stream.handle(User, events=["INSERT", "MODIFY"])
    def on_user_change(old: User | None, new: User | None) -> None: ...

    def lambda_handler(event, context):
        return stream.lambda_handler(event, context)
"""

import base64
import json
import logging
import signal
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from types import FrameType
from typing import Any, NamedTuple, NoReturn

from boto3.dynamodb.types import TypeDeserializer
from pydantic import BaseModel, ValidationError

from .events import (
    ALL_EVENTS,
    AttributeMap,
    BatchResponse,
    DynamoChangeEvent,
    EventName,
    KinesisEvent,
    KinesisEventRecord,
    LambdaContext,
)

log = logging.getLogger(__name__)

_deserializer = TypeDeserializer()

type Handler[M: BaseModel] = Callable[[M | None, M | None], None]
"""A change callback, given the item's old and new images (`None` where absent)."""


class HandlerNotFoundError(Exception):
    """No registered model and event pair accepts a change record."""


class UnhandledUserCodeException(Exception):
    """A registered handler raised while `raise_on_error` was set."""


class LambdaTimeoutException(Exception):
    """The Lambda invocation is about to time out."""


class ParsedChange(NamedTuple):
    """A change record validated against its registered model.

    Attributes:
        event_name: The kind of change made to the item
        model: The registered model the images were validated as
        old_image: The item before the change, absent for `INSERT`
        new_image: The item after the change, absent for `REMOVE`
        handler: The function registered for `model` and `event_name`
    """

    event_name: EventName
    model: type[BaseModel]
    old_image: BaseModel | None
    new_image: BaseModel | None
    handler: Handler[Any]


def _decode_change(record: KinesisEventRecord) -> DynamoChangeEvent:
    """Decode the DynamoDB change event carried in a Kinesis record's payload."""
    return json.loads(base64.b64decode(record["kinesis"]["data"]))


def _deserialize(image: AttributeMap | None) -> dict[str, Any] | None:
    """Convert an image from DynamoDB's typed JSON to plain Python values."""
    if image is None:
        return None
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


def _handler_name(handler: Handler[Any]) -> str:
    """Name a handler for log messages; not every callable has a `__name__`."""
    return getattr(handler, "__qualname__", repr(handler))


def _raise_timeout(_signum: int, _frame: FrameType | None) -> NoReturn:
    """Signal handler that interrupts processing when the Lambda deadline nears."""
    log.error("AWS Lambda timeout reached while processing Kinesis records")
    raise LambdaTimeoutException("AWS Lambda timeout reached")


@contextmanager
def _timeout_alarm(context: LambdaContext) -> Generator[None]:
    """Raise `LambdaTimeoutException` one second before the invocation times out.

    Relies on `SIGALRM`, so does nothing on platforms without it (e.g. Windows).
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    # Leave a second to report progress, but always arm the alarm for at least one
    seconds = max(1, context.get_remaining_time_in_millis() // 1000 - 1)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)


class DynamoStreamHandler:
    """Route DynamoDB changes from a Kinesis stream to handlers registered by model.

    Models are tried in registration order, so register more specific models first
    when several could validate the same item.
    """

    def __init__(self) -> None:
        """Create a handler with no registrations."""
        # Registered handlers per event, keyed by model in registration order
        self._handlers: dict[EventName, dict[type[BaseModel], Handler[Any]]] = {
            event: {} for event in ALL_EVENTS
        }

    def _match_model(
        self, event: EventName, data: dict[str, Any]
    ) -> tuple[type[BaseModel], BaseModel]:
        """Return the first model registered for `event` that validates `data`."""
        for model in self._handlers[event]:
            try:
                return model, model.model_validate(data)
            except ValidationError:
                continue

        raise HandlerNotFoundError(
            f"No registered model accepts the {event} change:\n[{data}]"
        )

    @staticmethod
    def _log_handler_failure(change: ParsedChange) -> None:
        """Log the exception being handled along with the images of the failed change."""
        message = (
            f"Handler {_handler_name(change.handler)} raised processing a {change.event_name}"
            f" change for {change.model.__name__}\n"
        )
        if change.old_image is not None:
            message += f"Old image:\n{change.old_image.model_dump_json(indent=4)}\n"
        if change.new_image is not None:
            message += f"New image:\n{change.new_image.model_dump_json(indent=4)}\n"

        log.exception(message)

    def add(self, other: DynamoStreamHandler) -> None:
        """Merge another handler's registrations into this one.

        Its registrations replace any of ours for the same model and event.
        """
        for event, handlers in other._handlers.items():
            self._handlers[event].update(handlers)

    def handle[M: BaseModel](
        self, model: type[M], events: Iterable[EventName] = ALL_EVENTS
    ) -> Callable[[Handler[M]], Handler[M]]:
        """Decorator registering a function to handle changes to items of `model`.

        Args:
            model: The model the changed item's images are validated as
            events: The change events to handle, all of them by default

        Returns:
            A decorator that registers and returns the function unchanged
        """

        def register(func: Handler[M]) -> Handler[M]:
            """Register `func` for each requested event."""
            for event in events:
                self._handlers[event][model] = func
            return func

        return register

    def parse_record(self, record: KinesisEventRecord) -> ParsedChange:
        """Decode a Kinesis record and validate its change against a registered model.

        The model is chosen by the old image when present, otherwise the new image, and
        the remaining image is then validated as that same model.

        Args:
            record: A Kinesis record carrying a DynamoDB change event

        Returns:
            The validated change and the handler registered for it

        Raises:
            HandlerNotFoundError: No registered model accepts the change, or the stream
                carries no item images (a `KEYS_ONLY` view)
            ValidationError: The second image does not validate as the chosen model
        """
        change = _decode_change(record)
        event = change["eventName"]
        if not self._handlers.get(event):
            raise HandlerNotFoundError(f"No handlers registered for {event} changes")

        old_data = _deserialize(change["dynamodb"].get("OldImage"))
        new_data = _deserialize(change["dynamodb"].get("NewImage"))

        # Pick the model from the first image present, then validate the other as it
        old_image = new_image = None
        if old_data is not None:
            model, old_image = self._match_model(event, old_data)
            if new_data is not None:
                new_image = model.model_validate(new_data)
        elif new_data is not None:
            model, new_image = self._match_model(event, new_data)
        else:
            raise HandlerNotFoundError(f"The {event} change carries no item images")

        return ParsedChange(
            event_name=event,
            model=model,
            old_image=old_image,
            new_image=new_image,
            handler=self._handlers[event][model],
        )

    def lambda_handler(
        self,
        event: KinesisEvent,
        context: LambdaContext,
        raise_on_error: bool = False,
    ) -> BatchResponse:
        """Process a batch of Kinesis records, calling the handler for each change.

        A handler's exception is logged and processing moves on, unless `raise_on_error`
        is set. If the invocation is about to time out, the unfinished record is reported
        as a batch item failure so Lambda retries from it.

        Args:
            event: The event Lambda was invoked with
            context: The Lambda context, used to schedule the timeout alarm
            raise_on_error: Stop and raise when a handler raises

        Returns:
            A partial batch response for the event source mapping

        Raises:
            HandlerNotFoundError: A record has no handler that accepts it
            UnhandledUserCodeException: A handler raised and `raise_on_error` is set
        """
        records = event["Records"]
        failures = 0
        log.info("DynamoDB stream handler invoked with %s records", len(records))

        # Sequence number of the record being processed, for reporting on timeout
        in_progress: str | None = None
        try:
            with _timeout_alarm(context):
                for position, record in enumerate(records, start=1):
                    in_progress = record["kinesis"]["sequenceNumber"]
                    change = self.parse_record(record)

                    log.info(
                        "Processing record %s/%s: %s change for %s",
                        position,
                        len(records),
                        change.event_name,
                        change.model.__name__,
                    )

                    try:
                        change.handler(change.old_image, change.new_image)
                    except LambdaTimeoutException:
                        # Let the timeout through rather than counting it as a failure
                        raise
                    except Exception as exc:
                        failures += 1
                        self._log_handler_failure(change)
                        if raise_on_error:
                            raise UnhandledUserCodeException(
                                f"Handler {_handler_name(change.handler)} raised"
                            ) from exc

                    in_progress = None

        except LambdaTimeoutException:
            # Ask Lambda to retry from the record that was cut short, if any
            if in_progress is None:
                return {"batchItemFailures": []}
            return {"batchItemFailures": [{"itemIdentifier": in_progress}]}

        log.info(
            "Processed %s/%s records successfully",
            len(records) - failures,
            len(records),
        )
        return {"batchItemFailures": []}
