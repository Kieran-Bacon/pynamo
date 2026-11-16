import base64
import json
import logging
import signal
from collections.abc import Callable, Generator
from typing import (
    Any,
    Literal,
)

from boto3.dynamodb.types import TypeDeserializer
from pydantic import BaseModel

log = logging.getLogger(__name__)

dynamoDeserializer = TypeDeserializer()


class HandlerNotFoundError(Exception):
    pass


class UnhandledUserCodeException(Exception):
    pass


class LambdaTimeoutException(Exception):
    pass


class DynamoStreamHandler:
    @staticmethod
    def _default_parse_exception_handler(e: Exception, data):

        log.exception(
            f"Parse exception encountered attempting to parse from kinesis DDB change stream\n[{data}]",
        )

    @staticmethod
    def _default_timeout_handler(_signal, _frame):
        """Handle imminent timeout"""
        log.error(
            "AWS Lambda timeout reached when attempting to process kinesis records"
        )

        raise LambdaTimeoutException("AWS Lambda timeout reached")

    def __init__(self) -> None:
        """Create a client for parsing dynamo db change events from a kinesis stream

        Args:
            Model (type[BaseModel]): The base model which will be used to parse dynamodb change events
        """

        self.parse_exception_handler = self._default_parse_exception_handler

        self._handler_dir: dict[tuple[type[BaseModel], str], Callable] = {}

        # self._supported_types: Set[type[BaseModel]] = set()

        self.timeout_handler = self._default_timeout_handler

        # current state
        self._last_processed_record = None
        self._current_record = None

    def add(self, other: DynamoStreamHandler):
        self._handler_dir.update(other._handler_dir)
        # self._supported_types.update(other._supported_types)

    def handler(
        self, _type: type[BaseModel], events=["INSERT", "MODIFY", "DELETE"]
    ) -> Callable[[Callable], Callable]:
        """Register to handle DynamoDB changes for a give data type and event types

        Args:
            _type (type[BaseModel]): The pydantic.BaseModel which should be used to parse the data model
            events (list, optional): The events types which are supported by this handler. Defaults to ["INSERT","MODIFY","DELETE"].
        """

        def wrapper(func: Callable):

            for event in events:
                self._handler_dir[(_type, event)] = func

            return func

        return wrapper

    def _attempt_parse(
        self, data: dict, event: str
    ) -> tuple[type[BaseModel], BaseModel, Callable]:
        # check each registered model to see if it can parse the data
        for (_type, event_name), handler_func in self._handler_dir.items():
            if event != event_name:
                continue
            try:
                return _type, _type.model_validate(data), handler_func
            except:
                pass

        raise HandlerNotFoundError(
            f"No supported handler found for event: {event} and data:\n[{data}]"
        )

    def parse_record(
        self, kinesis_record: dict
    ) -> tuple[str, BaseModel | None, BaseModel | None, Callable]:
        """Parse an individual kinesis record containing a DynamoDB change record

        Args:
            kinesis_record (Dict): a kinesis record containing a DynamoDB change record

        Returns:
            Tuple[Optional[BaseModel], Optional[BaseModel]]: a tuple containing the parsed `old_image` and `new_image`
        """

        old_image = None
        new_image = None

        record_model = None

        dynamo_data = json.loads(base64.b64decode(kinesis_record["kinesis"]["data"]))

        self._current_record = kinesis_record["kinesis"]["sequenceNumber"]

        event: Literal["INSERT", "MODIFY", "REMOVE"] = dynamo_data["eventName"]

        if event in ["REMOVE", "MODIFY"]:
            old_image_data = {
                k: dynamoDeserializer.deserialize(v)
                for k, v in dynamo_data["dynamodb"]["OldImage"].items()
            }

            if record_model is None:
                record_model, old_image, handler_func = self._attempt_parse(
                    old_image_data, event
                )
            else:
                old_image = record_model.model_validate(old_image_data)

        if event in ["INSERT", "MODIFY"]:
            new_image_data = {
                k: dynamoDeserializer.deserialize(v)
                for k, v in dynamo_data["dynamodb"]["NewImage"].items()
            }

            if record_model is None:
                record_model, new_image, handler_func = self._attempt_parse(
                    new_image_data, event
                )
            else:
                new_image = record_model.model_validate(new_image_data)

        return event, old_image, new_image, handler_func  # type: ignore (both cannot be None)

    def lambda_handler(self, event: dict, context: Any, raise_on_error=False) -> dict:
        """The entry point for the AWS Lambda function. This function is called by AWS
        Lambda when the function is invoked. It processes the records in the event and
        calls the appropriate handler for each record.

        Args:
            event (Dict): The AWS Lambda event generated by kinesis
            context (Dict): The AWS Lambda context generated by kinesis
        """
        total_records = len(event["Records"])
        record_processing_failures = 0

        log.info(
            "DynamoDB Stream Handler invoked with %s records",
            total_records,
        )

        try:
            # set the handler for the alarm
            if hasattr(signal, "SIGALRM"):
                signal.signal(signal.SIGALRM, self.timeout_handler)  # type: ignore - `signal.SIGALRM` is available on linux platforms

                # Setup alarm for remaining runtime minus a second
                signal.alarm(int(context.get_remaining_time_in_millis() / 1000) - 1)  # type: ignore - `signal.alarm` is available on linux platforms

            # Handle each record in the event
            for i, record in enumerate(event["Records"]):
                event_name, old_image, new_image, handler_func = self.parse_record(
                    record
                )

                log.info(
                    "Processing record %s/%s with event type [%s] for type [%s]",
                    i + 1,
                    total_records,
                    event_name,
                    handler_func.__name__,
                )

                try:
                    handler_func(old_image, new_image)
                except Exception:
                    record_processing_failures += 1
                    msg = f"User code exception encountered attempting to handle event type [{event_type}] for type [{_type}]\n"

                    if old_image is not None:
                        msg += f"Old Image:\n {old_image.model_dump_json(indent=4)}\n"

                    if new_image is not None:
                        msg += f"New Image:\n {new_image.model_dump_json(indent=4)}\n"

                    log.exception(msg)

                    if raise_on_error:
                        log.info(
                            "`raise_on_error` flag is `True`. Propagating exception and exiting the processing loop"
                        )
                        raise UnhandledUserCodeException()

                self._last_processed_record = self._current_record

        except LambdaTimeoutException:
            return {
                "batchItemFailures": [{"itemIdentifier": self._last_processed_record}]
            }

        log.info(
            "Record processing completed successfully for %s/%s records",
            total_records - record_processing_failures,
            total_records,
        )

        # deactivate the timeout alarm
        if hasattr(signal, "alarm"):
            signal.alarm(0)  # type: ignore - `signal.alarm` is available on linux platforms

        return {"batchItemFailures": []}
