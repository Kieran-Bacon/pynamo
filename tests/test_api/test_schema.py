"""Tests for building the AppSync schema from resolvers in `tidal.api`."""

import datetime
import decimal
import enum
import textwrap

import pydantic
import pytest
from pydantic.alias_generators import to_camel

from tidal.api import AppSyncEvent, SchemaError
from tidal.api.resolvers import Resolver
from tidal.api.schema import build_schema, print_sdl
from tidal.models import DynamoModel, Page, single_table_indexes


class Post(DynamoModel):
    """A user's post."""

    user_id: str
    post_id: str
    content: str

    class Meta:
        indexes = single_table_indexes(base=("USER#{user_id}", "POST#{post_id}"))


class User(DynamoModel):
    class Meta:
        indexes = single_table_indexes(base=("USER#{user_id}", "USER"))

    user_id: str
    name: str = pydantic.Field(description="Display name")
    age: int

    @property
    def posts(self) -> list[Post]:
        """The user's posts."""
        return []

    @property
    def internal(self):
        """Unannotated, so left out of the schema."""
        return None


class Colour(enum.Enum):
    """A colour."""

    RED = "red"
    GREEN = "green"


class Node(pydantic.BaseModel):
    """A model that references itself."""

    value: int
    children: list[Node] = []


def _sdl(*resolvers: Resolver) -> str:
    """Build a schema from resolvers with camelCase names and print it as SDL."""
    return print_sdl(build_schema(resolvers, to_camel))


def test_docs_example():
    def get_user_info(user_id: str, event: AppSyncEvent) -> User:
        """Get a user."""
        raise NotImplementedError

    def get_users(
        user_id: str, older_than: int | None, continuation_token: dict | None = None
    ) -> Page[User]:
        raise NotImplementedError

    def update_user(user_id: str, name: str | None = None, age: int = 0) -> User:
        raise NotImplementedError

    sdl = _sdl(
        Resolver(get_user_info, "Query", "getUserInfo", "Get a user."),
        Resolver(get_users, "Query", "getUsers"),
        Resolver(update_user, "Mutation", "updateUser", None, "Use editUser"),
    )

    assert sdl == (
        textwrap.dedent('''\
            type Query {
              """Get a user."""
              getUserInfo(userId: String!): User!
              getUsers(userId: String!, olderThan: Int, continuationToken: AWSJSON): UserPage!
            }

            type User {
              userId: String!

              """Display name"""
              name: String!
              age: Int!

              """The user's posts."""
              posts: [Post!]!
            }

            """A user's post."""
            type Post {
              userId: String!
              postId: String!
              content: String!
            }

            type UserPage {
              items: [User!]!
              token: AWSJSON
            }

            type Mutation {
              updateUser(userId: String!, name: String, age: Int): User! @deprecated(reason: "Use editUser")
            }''')
    )


def test_scalars_enums_and_recursive_models():
    class Reading(pydantic.BaseModel):
        at: datetime.datetime
        on: datetime.date
        amount: decimal.Decimal
        data: dict[str, int]
        tags: set[str]
        colour: Colour | None

    def reading(colour: Colour) -> Reading:
        raise NotImplementedError

    def tree() -> Node:
        raise NotImplementedError

    sdl = _sdl(Resolver(reading, "Query", "reading"), Resolver(tree, "Query", "tree"))

    # AppSync's own scalars are used but not declared
    assert "scalar AWS" not in sdl
    assert "at: AWSDateTime!" in sdl
    assert "on: AWSDate!" in sdl
    assert "amount: Float!" in sdl
    assert "data: AWSJSON!" in sdl
    assert "tags: [String!]!" in sdl
    # Enums are named after their members, and shared by inputs and outputs
    assert '"""A colour."""\nenum Colour {\n  RED\n  GREEN\n}' in sdl
    assert "colour: Colour\n" in sdl
    assert "reading(colour: Colour!): Reading!" in sdl
    assert "children: [Node!]!" in sdl


def test_model_arguments_become_input_types():
    class Address(pydantic.BaseModel):
        street_name: str

    class Profile(pydantic.BaseModel):
        address: Address

        @property
        def summary(self) -> str:
            return ""

    def save(profile: Profile) -> Profile:
        raise NotImplementedError

    sdl = _sdl(Resolver(save, "Mutation", "save"), Resolver(save, "Query", "q"))

    assert "input ProfileInput {\n  address: AddressInput!\n}" in sdl
    assert "input AddressInput {\n  streetName: String!\n}" in sdl
    # Properties are only on the output type
    assert "type Profile {\n  address: Address!\n  summary: String!\n}" in sdl
    assert "save(profile: ProfileInput!): Profile!" in sdl


def test_unsupported_field_names_its_path():
    class Holder(pydantic.BaseModel):
        value: int | str

    def holder() -> Holder:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="Holder.value: union types aren't supported"):
        build_schema([Resolver(holder, "Query", "holder")], to_camel)


def test_names_that_collide_are_rejected():
    class Clash(pydantic.BaseModel):
        user_id: str
        userId: str  # Deliberately collides with user_id once converted

    def clash() -> Clash:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="more than one field named 'userId'"):
        build_schema([Resolver(clash, "Query", "clash")], to_camel)


def test_a_query_is_required():
    def update() -> str:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="at least one query"):
        build_schema([Resolver(update, "Mutation", "update")], to_camel)
