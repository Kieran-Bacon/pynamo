import arrow
from typing import Any

import datetime

from typing_extensions import Annotated
from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler, BeforeValidator
from pydantic_core import core_schema
from pydantic.json_schema import JsonSchemaValue
import re


class _ArrowPydanticAnnotation:
    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        """
        We return a pydantic_core.CoreSchema that behaves in the following ways:

        * strings will be parsed as `arrow.Arrow` instances using `arrow.get()`
        * `arrow.Arrow`` instances will be parsed as `arrow.Arrow`` instances without any changes
        * Nothing else will pass validation
        * Serialization will always return `arrow.Arrow.isoformat()`
        """

        def validate_from_str(value: str) -> arrow.Arrow:
            return arrow.get(value)

        def validate_from_datetime(value: datetime.datetime) -> arrow.Arrow:
            return arrow.get(value)

        from_str_schema = core_schema.chain_schema(
            [
                core_schema.str_schema(),
                core_schema.no_info_plain_validator_function(validate_from_str),
            ]
        )

        from_datetime_schema = core_schema.chain_schema(
            [
                core_schema.datetime_schema(),
                core_schema.no_info_plain_validator_function(validate_from_datetime),
            ]
        )


        return core_schema.json_or_python_schema(
            json_schema=from_str_schema,
            python_schema=core_schema.union_schema(
                [
                    # check if it's an instance first before doing any further work
                    core_schema.is_instance_schema(arrow.Arrow),
                    from_datetime_schema,
                    from_str_schema,
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda instance: instance.isoformat()
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, _core_schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # Use the same schema that would be used for `int`
        return handler(core_schema.str_schema())

Arrow_MAX = arrow.get(2147483647)
Arrow_ISO8609 = Annotated[arrow.Arrow, _ArrowPydanticAnnotation]

emailRegex = re.compile('[^@]+@[^@]+\.[^@]+')
def email_address_validation(email: str) -> str:
    assert emailRegex.match(email)
    email = email.lower()
    return email

EmailAddress = Annotated[str, BeforeValidator(email_address_validation)]