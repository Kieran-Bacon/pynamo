"""Consume DynamoDB item changes streamed through Kinesis in AWS Lambda.

`events` types the Lambda and change payloads, and `stream_handler` the
`DynamoStreamHandler` that dispatches them. The public API is re-exported here.
"""

from .exceptions import (
    HandlerNotFoundError,
    LambdaTimeoutException,
    UnhandledUserCodeException,
)
from .stream_handler import (
    DynamoStreamHandler,
)

__all__ = [
    "DynamoStreamHandler",
    "HandlerNotFoundError",
    "LambdaTimeoutException",
    "UnhandledUserCodeException",
]
