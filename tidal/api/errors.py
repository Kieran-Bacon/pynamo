"""Exceptions raised while building a GraphQL schema or resolving an AppSync event.

AppSync reports an exception raised by a direct Lambda resolver to the client with the
exception's class name as the `errorType`, so these names are part of the API's contract.
"""


class GraphQLApiError(Exception):
    """Base class for every error raised by this package."""


class SchemaError(GraphQLApiError):
    """A resolver or model can't be represented in a GraphQL schema.

    Raised when the schema is built, e.g. for an unsupported annotation or a missing
    return annotation, so mistakes surface at deploy time rather than per request.
    """


class ResolverNotFoundError(GraphQLApiError):
    """An event names a type and field that no resolver is registered for."""


class ArgumentError(GraphQLApiError):
    """An event's arguments don't match the resolver's signature."""
