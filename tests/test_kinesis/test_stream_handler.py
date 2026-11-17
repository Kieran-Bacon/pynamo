"""Tests for dispatching Kinesis-delivered DynamoDB changes with `DynamoStreamHandler`."""

import base64
import json
import logging
from typing import Any
from unittest.mock import Mock

import pydantic
import pytest
from boto3.dynamodb.types import TypeSerializer

from pynamo.kinesis import (
    DynamoStreamHandler,
    HandlerNotFoundError,
    UnhandledUserCodeException,
)
from pynamo.kinesis.events import EventName, KinesisEventRecord

_serializer = TypeSerializer()


class User(pydantic.BaseModel):
    """An item recognised by its `email`."""

    user_id: str
    email: str


class Order(pydantic.BaseModel):
    """An item recognised by its `total`."""

    order_id: str
    total: int


def _image(item: dict[str, Any]) -> dict[str, Any]:
    """Serialise a plain item into DynamoDB's typed JSON."""
    return {key: _serializer.serialize(value) for key, value in item.items()}


def _record(
    event: EventName,
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    sequence_number: str = "1",
) -> KinesisEventRecord:
    """Build a Kinesis record carrying a DynamoDB change of the given images."""
    dynamodb: dict[str, Any] = {"Keys": {}}
    if old is not None:
        dynamodb["OldImage"] = _image(old)
    if new is not None:
        dynamodb["NewImage"] = _image(new)

    payload = {"eventName": event, "dynamodb": dynamodb}
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {
        "kinesis": {
            "kinesisSchemaVersion": "1.0",
            "partitionKey": "key",
            "sequenceNumber": sequence_number,
            "data": data,
            "approximateArrivalTimestamp": 0.0,
        },
        "eventSource": "aws:kinesis",
        "eventVersion": "1.0",
        "eventID": f"shardId-000:{sequence_number}",
        "eventName": "aws:kinesis:record",
        "invokeIdentityArn": "arn:aws:iam::123456789012:role/test",
        "awsRegion": "eu-west-2",
        "eventSourceARN": "arn:aws:kinesis:eu-west-2:123456789012:stream/test",
    }


@pytest.fixture
def stream() -> DynamoStreamHandler:
    """A stream handler with no handlers registered."""
    return DynamoStreamHandler()


@pytest.fixture
def context() -> Mock:
    """A Lambda context with a minute remaining."""
    return Mock(get_remaining_time_in_millis=Mock(return_value=60_000))


def test_dispatches_to_the_model_that_validates_the_item(stream, context):
    on_user, on_order = Mock(__name__="on_user"), Mock(__name__="on_order")
    stream.handle(User)(on_user)
    stream.handle(Order)(on_order)

    response = stream.lambda_handler(
        {"Records": [_record("INSERT", new={"order_id": "o1", "total": 3})]}, context
    )

    assert response == {"batchItemFailures": []}
    on_user.assert_not_called()
    on_order.assert_called_once_with(None, Order(order_id="o1", total=3))


def test_modify_passes_both_images(stream, context):
    handler = Mock(__name__="handler")
    stream.handle(User, events=["MODIFY"])(handler)

    old = {"user_id": "u1", "email": "a@x"}
    new = {"user_id": "u1", "email": "b@x"}
    stream.lambda_handler({"Records": [_record("MODIFY", old=old, new=new)]}, context)

    handler.assert_called_once_with(User(**old), User(**new))


def test_remove_passes_only_the_old_image(stream, context):
    handler = Mock(__name__="handler")
    stream.handle(User)(handler)

    old = {"user_id": "u1", "email": "a@x"}
    stream.lambda_handler({"Records": [_record("REMOVE", old=old)]}, context)

    handler.assert_called_once_with(User(**old), None)


def test_unregistered_event_raises(stream):
    stream.handle(User, events=["INSERT"])(Mock(__name__="handler"))

    with pytest.raises(HandlerNotFoundError):
        stream.parse_record(_record("REMOVE", old={"user_id": "u1", "email": "a@x"}))


def test_unrecognised_item_raises(stream):
    stream.handle(User)(Mock(__name__="handler"))

    with pytest.raises(HandlerNotFoundError):
        stream.parse_record(_record("INSERT", new={"order_id": "o1"}))


def test_handler_failures_are_logged_and_skipped(stream, context, caplog):
    failing = Mock(__name__="failing", side_effect=RuntimeError("boom"))
    stream.handle(User)(failing)

    records = [
        _record("INSERT", new={"user_id": "u1", "email": "a@x"}, sequence_number="1"),
        _record("INSERT", new={"user_id": "u2", "email": "b@x"}, sequence_number="2"),
    ]
    with caplog.at_level(logging.ERROR, logger="pynamo.kinesis.stream_handler"):
        response = stream.lambda_handler({"Records": records}, context)

    assert response == {"batchItemFailures": []}
    assert failing.call_count == 2
    assert caplog.records


def test_raise_on_error_propagates_handler_failures(stream, context, caplog):
    stream.handle(User)(Mock(__name__="failing", side_effect=RuntimeError))

    record = _record("INSERT", new={"user_id": "u1", "email": "a@x"})
    with (
        caplog.at_level(logging.ERROR, logger="pynamo.kinesis.stream_handler"),
        pytest.raises(UnhandledUserCodeException),
    ):
        stream.lambda_handler({"Records": [record]}, context, raise_on_error=True)
    assert caplog.records


def test_add_merges_registrations_per_event(stream):
    on_user, on_order = Mock(__name__="on_user"), Mock(__name__="on_order")
    stream.handle(User)(on_user)
    other = DynamoStreamHandler()
    other.handle(Order)(on_order)

    stream.add(other)

    user = stream.parse_record(_record("INSERT", new={"user_id": "u1", "email": "a@x"}))
    order = stream.parse_record(_record("INSERT", new={"order_id": "o1", "total": 3}))
    assert user.handler is on_user
    assert order.handler is on_order
