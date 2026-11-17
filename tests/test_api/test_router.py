"""Tests for registering resolvers and dispatching events with `tidal.api`."""

from typing import Any

import pydantic
import pytest

from tidal.api import (
    AppSyncEvent,
    ArgumentError,
    ResolverNotFoundError,
    Router,
    SchemaError,
)
from tidal.models import Page


class Post(pydantic.BaseModel):
    content: str


class User(pydantic.BaseModel):
    user_id: str
    name: str
    age: int

    @property
    def posts(self) -> list[Post]:
        """The user's posts."""
        return [Post(content=f"by {self.name}")]


_USERS = {"u1": User(user_id="u1", name="Ada", age=36)}


def _event(
    parent: str,
    field: str,
    arguments: dict[str, Any],
    selection: list[str],
    identity: Any = None,
) -> AppSyncEvent:
    """Create an AppSync event for a field, with the given arguments and selection."""
    return {
        "arguments": arguments,
        "identity": identity,
        "source": None,
        "request": {"headers": {}},
        "info": {
            "parentTypeName": parent,
            "fieldName": field,
            "variables": {},
            "selectionSetList": selection,
            "selectionSetGraphQL": "",
        },
        "prev": None,
        "stash": {},
    }


@pytest.fixture
def router() -> Router:
    """A router resembling the docs example, over an in-memory store."""
    router = Router()

    @router.query()
    def get_user(user_id: str) -> User:
        """Get a user by ID."""
        return _USERS[user_id]

    @router.query()
    def get_users(older_than: int | None, token: dict | None = None) -> Page[User]:
        users = [u for u in _USERS.values() if older_than is None or u.age > older_than]
        return Page(items=users, token=None)

    @router.mutation(deprecation_reason="Use renameUser")
    def update_user(user_id: str, name: str | None = None) -> User:
        user = _USERS[user_id].model_copy()
        if name is not None:
            user.name = name
        return user

    @router.query()
    def whoami(event: AppSyncEvent) -> str:
        identity: Any = event["identity"]
        return identity["username"]

    return router


# Registration


def test_decorators_return_the_function():
    router = Router()

    def ping() -> str:
        return "pong"

    assert router.query()(ping) is ping
    assert ping() == "pong"


def test_names_descriptions_and_deprecation(router):
    sdl = router.sdl()

    assert '"""Get a user by ID."""\n  getUser(userId: String!): User!' in sdl
    assert "getUsers(olderThan: Int, token: AWSJSON): UserPage!" in sdl
    assert (
        'updateUser(userId: String!, name: String): User! @deprecated(reason: "Use renameUser")'
        in sdl
    )
    # The event parameter isn't an argument
    assert "whoami: String!" in sdl


def test_explicit_names_and_no_converter():
    router = Router(name_converter=None)

    @router.query("fetchUser", description="Fetch one.")
    def get_user(user_id: str) -> User:
        raise NotImplementedError

    sdl = router.sdl()
    assert '"""Fetch one."""\n  fetchUser(user_id: String!): User!' in sdl
    assert "user_id: String!" in sdl


def test_duplicate_names_are_rejected():
    router = Router()

    @router.query()
    def get_user() -> str:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="'getUser' on Query"):
        router.query("getUser")(get_user)


def test_async_resolvers_are_rejected():
    router = Router()

    async def get_user() -> str:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="must not be async"):
        router.query()(get_user)


def test_schema_is_rebuilt_after_registering(router):
    first = router.schema()
    assert router.schema() is first

    @router.query()
    def ping() -> str:
        return "pong"

    assert router.schema() is not first
    assert "ping: String!" in router.sdl()


# Including routers


def test_included_resolvers_are_merged(router):
    posts = Router()

    @posts.query()
    def get_post() -> Post:
        return Post(content="hello")

    router.include_router(posts)

    assert "getPost: Post!" in router.sdl()
    assert router(_event("Query", "getPost", {}, ["content"])) == {"content": "hello"}


def test_included_names_must_not_clash(router):
    other = Router()

    @other.query()
    def get_user() -> str:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="'getUser'"):
        router.include_router(other)


def test_included_name_converters_must_match(router):
    with pytest.raises(SchemaError, match="same name converter"):
        router.include_router(Router(name_converter=None))


# Dispatching events


def test_query_with_nested_selection(router):
    result = router(
        _event(
            "Query",
            "getUser",
            {"userId": "u1"},
            ["userId", "name", "posts", "posts/content"],
        ),
        None,
    )

    assert result == {"userId": "u1", "name": "Ada", "posts": [{"content": "by Ada"}]}


def test_page_result(router):
    result = router(
        _event("Query", "getUsers", {"olderThan": 40}, ["items", "items/name"])
    )

    assert result == {"items": []}


def test_mutation_with_default(router):
    renamed = router(
        _event("Mutation", "updateUser", {"userId": "u1", "name": "Bo"}, ["name"])
    )
    unchanged = router(_event("Mutation", "updateUser", {"userId": "u1"}, ["name"]))

    assert renamed == {"name": "Bo"}
    assert unchanged == {"name": "Ada"}


def test_event_is_passed_to_its_parameter(router):
    result = router(_event("Query", "whoami", {}, [], identity={"username": "ada"}))

    assert result == "ada"


def test_unknown_field(router):
    with pytest.raises(ResolverNotFoundError, match="Query.missing"):
        router(_event("Query", "missing", {}, []))


def test_fields_of_other_types_are_not_resolved(router):
    with pytest.raises(ResolverNotFoundError):
        router(_event("User", "getUser", {"userId": "u1"}, []))


def test_invalid_arguments(router):
    with pytest.raises(ArgumentError, match="'userId' is required"):
        router(_event("Query", "getUser", {}, ["name"]))
