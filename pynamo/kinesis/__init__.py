"""Consume DynamoDB item changes streamed through Kinesis in AWS Lambda.

`events` types the Lambda and change payloads, and `stream_handler` the
`DynamoStreamHandler` that dispatches them. The public API is re-exported here.
"""

from .events import (
    ALL_EVENTS,
    BatchResponse,
    DynamoChangeEvent,
    EventName,
    KinesisEvent,
    KinesisEventRecord,
    LambdaContext,
)
from .stream_handler import (
    DynamoStreamHandler,
    Handler,
    HandlerNotFoundError,
    LambdaTimeoutException,
    ParsedChange,
    UnhandledUserCodeException,
)

__all__ = [
    "ALL_EVENTS",
    "BatchResponse",
    "DynamoChangeEvent",
    "DynamoStreamHandler",
    "EventName",
    "Handler",
    "HandlerNotFoundError",
    "KinesisEvent",
    "KinesisEventRecord",
    "LambdaContext",
    "LambdaTimeoutException",
    "ParsedChange",
    "UnhandledUserCodeException",
]
