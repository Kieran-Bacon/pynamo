"""Typed shapes of the events AppSync sends to a Lambda function used as a direct resolver.

AppSync invokes the function once per resolved field with an `AppSyncEvent`. The event
says which field is being resolved (`info`), the arguments it was given, the caller's
`identity` and, for fields on a type other than `Query` or `Mutation`, the parent object
(`source`). What the function returns becomes the field's value.
"""

from typing import Any, NotRequired, TypedDict


class CognitoIdentity(TypedDict):
    """The caller of an API authorised by a Cognito user pool."""

    sub: str
    issuer: str
    username: str
    claims: dict[str, Any]
    sourceIp: list[str]
    defaultAuthStrategy: str
    groups: list[str] | None


class OidcIdentity(TypedDict):
    """The caller of an API authorised by an OpenID Connect provider."""

    sub: str
    issuer: str
    claims: dict[str, Any]


class IamIdentity(TypedDict):
    """The caller of an API authorised by AWS IAM."""

    accountId: str
    cognitoIdentityPoolId: str | None
    cognitoIdentityId: str | None
    sourceIp: list[str]
    username: str
    userArn: str
    cognitoIdentityAuthType: str | None
    cognitoIdentityAuthProvider: str | None


class LambdaIdentity(TypedDict):
    """The caller of an API authorised by a Lambda authoriser."""

    resolverContext: dict[str, Any]


type Identity = CognitoIdentity | OidcIdentity | IamIdentity | LambdaIdentity
"""The caller, in the form of the API's authorisation mode; API key callers have none."""


class AppSyncInfo(TypedDict):
    """Which field is being resolved and what the request selected from it."""

    parentTypeName: str
    fieldName: str
    variables: dict[str, Any]
    selectionSetList: list[str]
    selectionSetGraphQL: str


class AppSyncRequest(TypedDict):
    """The HTTP request the GraphQL operation arrived in."""

    headers: dict[str, str]
    domainName: NotRequired[str | None]


class PreviousResult(TypedDict):
    """The result of the previous function in a pipeline resolver."""

    result: Any


class AppSyncEvent(TypedDict):
    """The event AppSync invokes a direct Lambda resolver with, for a single field."""

    arguments: dict[str, Any]
    identity: Identity | None
    source: dict[str, Any] | None
    request: AppSyncRequest
    info: AppSyncInfo
    prev: PreviousResult | None
    stash: dict[str, Any]
