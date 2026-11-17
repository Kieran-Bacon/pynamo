"""Tests for building the AppSync schema from resolvers in `pynamo.new_graphql_api`."""

import datetime
import decimal
import enum
import textwrap
import unittest

import pydantic
from pydantic.alias_generators import to_camel

from pynamo.dynamo import DynamoModel, Page, single_table_indexes
from pynamo.new_graphql_api import AppSyncEvent, SchemaError
from pynamo.new_graphql_api.resolvers import Resolver
from pynamo.new_graphql_api.schema import ID, build_schema, print_sdl


class Post(DynamoModel):
    """A user's post."""

    indexes = single_table_indexes(base=("USER#{user_id}", "POST#{post_id}"))

    user_id: str
    post_id: str
    content: str


class User(DynamoModel):
    indexes = single_table_indexes(base=("USER#{user_id}", "USER"))

    user_id: ID
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


class SchemaTests(unittest.TestCase):
    def _schema_error(self, *resolvers: Resolver) -> str:
        """Build a schema that must fail, returning the error's message."""
        with self.assertRaises(SchemaError) as context:
            build_schema(resolvers, to_camel)
        return str(context.exception)

    def test_docs_example(self):
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

        self.assertEqual(
            sdl,
            textwrap.dedent('''\
                type Query {
                  """Get a user."""
                  getUserInfo(userId: String!): User!
                  getUsers(userId: String!, olderThan: Int, continuationToken: AWSJSON): UserPage!
                }

                type User {
                  userId: ID!

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
                }'''),
        )

    def test_scalars_enums_and_recursive_models(self):
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

        sdl = _sdl(
            Resolver(reading, "Query", "reading"), Resolver(tree, "Query", "tree")
        )

        # AppSync's own scalars are used but not declared
        self.assertNotIn("scalar AWS", sdl)
        self.assertIn("at: AWSDateTime!", sdl)
        self.assertIn("on: AWSDate!", sdl)
        self.assertIn("amount: Float!", sdl)
        self.assertIn("data: AWSJSON!", sdl)
        self.assertIn("tags: [String!]!", sdl)
        # Enums are named after their members, and shared by inputs and outputs
        self.assertIn('"""A colour."""\nenum Colour {\n  RED\n  GREEN\n}', sdl)
        self.assertIn("colour: Colour\n", sdl)
        self.assertIn("reading(colour: Colour!): Reading!", sdl)
        self.assertIn("children: [Node!]!", sdl)

    def test_model_arguments_become_input_types(self):
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

        self.assertIn("input ProfileInput {\n  address: AddressInput!\n}", sdl)
        self.assertIn("input AddressInput {\n  streetName: String!\n}", sdl)
        # Properties are only on the output type
        self.assertIn("type Profile {\n  address: Address!\n  summary: String!\n}", sdl)
        self.assertIn("save(profile: ProfileInput!): Profile!", sdl)

    def test_resolver_needs_a_return_annotation(self):
        def missing(user_id: str):
            raise NotImplementedError

        message = self._schema_error(Resolver(missing, "Query", "missing"))
        self.assertIn("must annotate the type it returns", message)

    def test_arguments_need_annotations(self):
        def untyped(user_id) -> str:
            raise NotImplementedError

        message = self._schema_error(Resolver(untyped, "Query", "untyped"))
        self.assertIn("'user_id' has no type annotation", message)

    def test_unsupported_field_names_its_path(self):
        class Holder(pydantic.BaseModel):
            value: int | str

        def holder() -> Holder:
            raise NotImplementedError

        message = self._schema_error(Resolver(holder, "Query", "holder"))
        self.assertIn("Holder.value: union types aren't supported", message)

    def test_names_that_collide_are_rejected(self):
        class Clash(pydantic.BaseModel):
            user_id: str
            userId: str  # Deliberately collides with user_id once converted

        def clash() -> Clash:
            raise NotImplementedError

        message = self._schema_error(Resolver(clash, "Query", "clash"))
        self.assertIn("more than one field named 'userId'", message)

    def test_a_query_is_required(self):
        def update() -> str:
            raise NotImplementedError

        message = self._schema_error(Resolver(update, "Mutation", "update"))
        self.assertIn("at least one query", message)


if __name__ == "__main__":
    unittest.main()
