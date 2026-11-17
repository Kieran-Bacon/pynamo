"""Typed shapes of the payloads exchanged with AWS Lambda for DynamoDB changes on Kinesis.

A table with a Kinesis streaming destination publishes each item change as a
`DynamoChangeEvent`, base64 encoded into a `KinesisEventRecord`. Lambda delivers those
records in batches as a `KinesisEvent` and accepts a `BatchResponse` naming any records
that must be retried.
"""

from typing import Any, Literal, NotRequired, Protocol, TypedDict

type EventName = Literal["INSERT", "MODIFY", "REMOVE"]
"""The kind of change made to a DynamoDB item."""

ALL_EVENTS: tuple[EventName, ...] = ("INSERT", "MODIFY", "REMOVE")
"""Every DynamoDB change event name."""

type AttributeMap = dict[str, dict[str, Any]]
"""An item in DynamoDB's typed JSON form, e.g. `{"id": {"S": "abc"}}`."""


class DynamoStreamRecord(TypedDict):
    """The item data of a change, as found under a change event's `dynamodb` key.

    Which images are present depends on the change and the stream's view type.
    """

    ApproximateCreationDateTime: NotRequired[int]
    ApproximateCreationDateTimePrecision: NotRequired[str]
    Keys: AttributeMap
    NewImage: NotRequired[AttributeMap]
    OldImage: NotRequired[AttributeMap]
    SizeBytes: NotRequired[int]


class DynamoChangeEvent(TypedDict):
    """A single DynamoDB item change, as published to a Kinesis data stream."""

    awsRegion: str
    eventID: str
    eventName: EventName
    eventSource: str
    recordFormat: str
    tableName: str
    userIdentity: NotRequired[dict[str, Any] | None]
    dynamodb: DynamoStreamRecord


class KinesisData(TypedDict):
    """The Kinesis portion of a record; `data` holds the base64 encoded payload."""

    kinesisSchemaVersion: str
    partitionKey: str
    sequenceNumber: str
    data: str
    approximateArrivalTimestamp: float


class KinesisEventRecord(TypedDict):
    """One record of a Kinesis batch delivered to a Lambda function."""

    kinesis: KinesisData
    eventSource: str
    eventVersion: str
    eventID: str
    eventName: str
    invokeIdentityArn: str
    awsRegion: str
    eventSourceARN: str


class KinesisEvent(TypedDict):
    """The event Lambda invokes a Kinesis consumer with."""

    Records: list[KinesisEventRecord]


class BatchItemFailure(TypedDict):
    """A record to retry, identified by its Kinesis sequence number."""

    itemIdentifier: str


class BatchResponse(TypedDict):
    """A partial batch response; Lambda retries from the earliest listed failure.

    Requires `ReportBatchItemFailures` to be enabled on the event source mapping.
    """

    batchItemFailures: list[BatchItemFailure]


class LambdaContext(Protocol):
    """The part of the Lambda context object the stream handler relies on."""

    def get_remaining_time_in_millis(self) -> int:
        """Return the milliseconds left before Lambda terminates the invocation."""
        ...
