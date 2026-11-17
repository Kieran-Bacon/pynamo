"""Tests for registering resolvers and dispatching events with `pynamo.new_graphql_api`."""

import unittest
from typing import Any

import pydantic

from pynamo.dynamo import Page
from pynamo.new_graphql_api import (
    AppSyncEvent,
    ArgumentError,
    ResolverNotFoundError,
    Router,
    SchemaError,
)


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


def _user_router() -> Router:
    """Create a router resembling the docs example, over an in-memory store."""
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


class RegistrationTests(unittest.TestCase):
    def test_decorators_return_the_function(self):
        router = Router()

        def ping() -> str:
            return "pong"

        self.assertIs(router.query()(ping), ping)
        self.assertEqual(ping(), "pong")

    def test_names_descriptions_and_deprecation(self):
        sdl = _user_router().sdl()

        self.assertIn('"""Get a user by ID."""\n  getUser(userId: String!): User!', sdl)
        self.assertIn("getUsers(olderThan: Int, token: AWSJSON): UserPage!", sdl)
        self.assertIn(
            'updateUser(userId: String!, name: String): User! @deprecated(reason: "Use renameUser")',
            sdl,
        )
        # The event parameter isn't an argument
        self.assertIn("whoami: String!", sdl)

    def test_explicit_names_and_no_converter(self):
        router = Router(name_converter=None)

        @router.query("fetchUser", description="Fetch one.")
        def get_user(user_id: str) -> User:
            raise NotImplementedError

        sdl = router.sdl()
        self.assertIn('"""Fetch one."""\n  fetchUser(user_id: String!): User!', sdl)
        self.assertIn("user_id: String!", sdl)

    def test_duplicate_names_are_rejected(self):
        router = Router()

        @router.query()
        def get_user() -> str:
            raise NotImplementedError

        with self.assertRaisesRegex(SchemaError, "'getUser' on Query"):
            router.query("getUser")(get_user)

    def test_async_resolvers_are_rejected(self):
        router = Router()

        async def get_user() -> str:
            raise NotImplementedError

        with self.assertRaisesRegex(SchemaError, "must not be async"):
            router.query()(get_user)

    def test_schema_is_rebuilt_after_registering(self):
        router = _user_router()
        first = router.schema()
        self.assertIs(router.schema(), first)

        @router.query()
        def ping() -> str:
            return "pong"

        self.assertIsNot(router.schema(), first)
        self.assertIn("ping: String!", router.sdl())


class IncludeRouterTests(unittest.TestCase):
    def test_resolvers_are_merged(self):
        posts = Router()

        @posts.query()
        def get_post() -> Post:
            return Post(content="hello")

        router = _user_router()
        router.include_router(posts)

        self.assertIn("getPost: Post!", router.sdl())
        self.assertEqual(
            router(_event("Query", "getPost", {}, ["content"])), {"content": "hello"}
        )

    def test_clashing_names_are_rejected(self):
        other = Router()

        @other.query()
        def get_user() -> str:
            raise NotImplementedError

        with self.assertRaisesRegex(SchemaError, "'getUser'"):
            _user_router().include_router(other)

    def test_name_converters_must_match(self):
        with self.assertRaisesRegex(SchemaError, "same name converter"):
            _user_router().include_router(Router(name_converter=None))


class DispatchTests(unittest.TestCase):
    def test_query_with_nested_selection(self):
        result = _user_router()(
            _event(
                "Query",
                "getUser",
                {"userId": "u1"},
                ["userId", "name", "posts", "posts/content"],
            ),
            None,
        )

        self.assertEqual(
            result,
            {"userId": "u1", "name": "Ada", "posts": [{"content": "by Ada"}]},
        )

    def test_page_result(self):
        result = _user_router()(
            _event("Query", "getUsers", {"olderThan": 40}, ["items", "items/name"])
        )

        self.assertEqual(result, {"items": []})

    def test_mutation_with_default(self):
        router = _user_router()

        renamed = router(
            _event("Mutation", "updateUser", {"userId": "u1", "name": "Bo"}, ["name"])
        )
        unchanged = router(_event("Mutation", "updateUser", {"userId": "u1"}, ["name"]))

        self.assertEqual(renamed, {"name": "Bo"})
        self.assertEqual(unchanged, {"name": "Ada"})

    def test_event_is_passed_to_its_parameter(self):
        result = _user_router()(
            _event("Query", "whoami", {}, [], identity={"username": "ada"})
        )

        self.assertEqual(result, "ada")

    def test_unknown_field(self):
        with self.assertRaisesRegex(ResolverNotFoundError, "Query.missing"):
            _user_router()(_event("Query", "missing", {}, []))

    def test_fields_of_other_types_are_not_resolved(self):
        with self.assertRaises(ResolverNotFoundError):
            _user_router()(_event("User", "getUser", {"userId": "u1"}, []))

    def test_invalid_arguments(self):
        with self.assertRaisesRegex(ArgumentError, "'userId' is required"):
            _user_router()(_event("Query", "getUser", {}, ["name"]))


if __name__ == "__main__":
    unittest.main()
