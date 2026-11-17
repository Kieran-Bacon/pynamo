"""Serve pydantic models through AWS AppSync, with Lambda as a direct resolver.

A work-in-progress replacement for `pynamo.graphql_api`. `events` types the AppSync
Lambda event, `errors` the exceptions raised, `resolvers` the functions resolving root
fields, `schema` the GraphQL schema built from them, `codec` the conversion of
arguments and results, and `router` the `Router` tying them together. The public API
is re-exported here.
"""

from .errors import ArgumentError, GraphQLApiError, ResolverNotFoundError, SchemaError
from .events import (
    AppSyncEvent,
    AppSyncInfo,
    AppSyncRequest,
    CognitoIdentity,
    IamIdentity,
    Identity,
    LambdaIdentity,
    OidcIdentity,
    PreviousResult,
)
from .router import Router
from .schema import ID, NameConverter

__all__ = [
    "ID",
    "AppSyncEvent",
    "AppSyncInfo",
    "AppSyncRequest",
    "ArgumentError",
    "CognitoIdentity",
    "GraphQLApiError",
    "IamIdentity",
    "Identity",
    "LambdaIdentity",
    "NameConverter",
    "OidcIdentity",
    "PreviousResult",
    "ResolverNotFoundError",
    "Router",
    "SchemaError",
]
