import json
import logging
import re
from typing import Any, Dict, Tuple

import graphql
from aws_lambda_powertools.utilities.data_classes import AppSyncResolverEvent
from aws_lambda_powertools.utilities.data_classes.appsync_resolver_event import (
    AppSyncIdentityCognito,
    AppSyncIdentityIAM,
)
from graphql import parse, validate

from ..exceptions import GraphQLQueryValidationError
from ..graphql_router import (
    GraphQLEnumType,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLNonNull,
    GraphQLRouter,
)

log = logging.getLogger(__name__)


class AppSyncConnector:
    def __init__(self, router: GraphQLRouter):

        self.router = router

    def _resolveType(self, return_type, value) -> tuple[Any, Any]:
        """Compare provided argument against the expected argument type - recursively evaluate values and return their
        type values
        """

        # The field wrapper must first be removed
        if isinstance(return_type, GraphQLInputField):
            return self._resolveType(return_type.type, value)

        # If it is nullable - test for null and remove wrapper if concrete
        if isinstance(return_type, GraphQLNonNull):
            if value is None:
                return None, None

            else:
                return self._resolveType(return_type.of_type, value)

        # If the argument is a complex object
        if isinstance(return_type, GraphQLInputObjectType):
            if not isinstance(value, dict):
                raise ValueError(
                    "Incorrect type - expected object of type {return_type.name} received {value}"
                )

            valueTypes = {}
            for objectName, objectValue in value.items():
                mappingTypes, _ = self._resolveType(
                    return_type.fields[objectName], objectValue
                )
                valueTypes[objectName] = mappingTypes

            return valueTypes, value

        return return_type, value

    def _convertTypedArgumentsToSignature(
        self, typedArguments: dict[str, tuple[Any, Any]]
    ):

        parameters = []

        for argumentName, (_type, value) in typedArguments.items():
            if isinstance(_type, dict):
                # Unpack the keys inside the dict and pair the types against the values again for the next comparison
                subTypedArguments = {
                    key: (val, value[key]) for key, val in _type.items()
                }
                sig = (
                    argumentName
                    + ": { "
                    + self._convertTypedArgumentsToSignature(subTypedArguments)
                    + " }"
                )
                parameters.append(sig)

            elif isinstance(_type, GraphQLEnumType):
                parameters.append(f"{argumentName}: {value}")

            else:
                # All default types are handled as if they were json parameters
                parameters.append(f"{argumentName}: {json.dumps(value)}")

        return ", ".join(parameters)

    def _parse(self, event: dict) -> str:

        schema = self.router.schema()

        event_info = event["info"]
        # Fetch the object holding either all the queries or or the mutations

        try:
            targetType = schema.get_type(event_info["parentTypeName"])
            field = targetType.fields[event_info["fieldName"]]
        except Exception as e:
            raise GraphQLQueryValidationError(
                f"Requested Field: {event_info['fieldName']} could not be found"
            ) from e

        arguments = event.get("arguments", {})

        # Since the arguments can be empty (e.g getUser {name}) we have to avoid putting () on the query as this is an error
        querySignature = ""
        if arguments:
            try:
                typedArguments = {
                    argument: self._resolveType(field.args[argument].type, value)
                    for argument, value in arguments.items()
                }
            except Exception as e:
                raise GraphQLQueryValidationError(
                    f"Arguments {arguments} types do not match signature"
                ) from e

            # For each arugment provided, they get converted into the signature and joined with ','
            # An argument will always be name: <something>
            # The something needs to be resolved in the same way for dicts here so this is a recursive function

            query_args = self._convertTypedArgumentsToSignature(typedArguments)

            # # we need to tuen a dictionary into a list of function arguments, keeping quotes aroud string values but removing the quotes from the argument names.
            # # query_args = (", ").join([f"{key}: {val}" for key, val in event_info['variables'].items()])
            # query_args = re.sub(r'(?<!: )"(\S*?)"', '\\1', json.dumps(arguments))[1:-1]
            querySignature = f"({query_args})"

        field_name = event_info["fieldName"]
        queryType = event_info["parentTypeName"].lower()
        query = f"""
                {queryType} {{
                    {field_name}{querySignature}
                    {event_info["selectionSetGraphQL"]}

                }}
                """

        return query

    def __call__(self, event: dict, context) -> Any:

        query = self._parse(event)

        # TODO restore validation check
        val_errors = validate(self.router.schema(), parse(query))
        if len(val_errors) > 0:
            error_explain = "\n".join([str(e) for e in val_errors])
            raise GraphQLQueryValidationError(
                f"The query [{query}] failed schema validation: \n{error_explain}"
            )

        self.router.current_event = event

        result = self.router.resolve(query)

        if result.errors is not None:
            log.exception("query=%s resulted in errors=%s", query, result.errors)
            raise result.errors[0]

        else:
            log.debug("Successfully resolved query=%s data=%s", query, result.data)

            response = result.data[event["info"]["fieldName"]]
            return response
