"""Tests for decoding arguments and encoding results in `pynamo.new_graphql_api`."""

import datetime
import decimal
import enum
import unittest

import pydantic
from pydantic.alias_generators import to_camel

from pynamo.dynamo import Page
from pynamo.new_graphql_api import ArgumentError
from pynamo.new_graphql_api.codec import decode_arguments, encode_result
from pynamo.new_graphql_api.resolvers import Resolver


class Colour(enum.Enum):
    RED = "red"
    GREEN = "green"


class Address(pydantic.BaseModel):
    street_name: str


class Profile(pydantic.BaseModel):
    # The alias is the stored attribute name, which GraphQL doesn't use
    user_id: str = pydantic.Field(alias="PK")
    colour: Colour
    address: Address
    born: datetime.datetime
    score: decimal.Decimal
    extra: dict | None = None


class Post(pydantic.BaseModel):
    content: str


class User(pydantic.BaseModel):
    user_id: str
    colour: Colour
    score: decimal.Decimal
    data: dict
    created: datetime.datetime
    post_count: int = 0

    @property
    def posts(self) -> list[Post]:
        """Counts its calls, to show unselected properties aren't read."""
        self.post_count += 1
        return [Post(content="first"), Post(content="second")]


def find_users(
    user_id: str,
    older_than: int | None,
    limit: int = 10,
    name: str | None = "anyone",
) -> list[User]:
    """A resolver whose arguments cover each way an argument can be left out."""
    raise NotImplementedError


def save_profile(profile: Profile, colours: list[Colour]) -> Profile:
    """A resolver taking an input object and a list of enums."""
    raise NotImplementedError


_FIND_USERS = Resolver(find_users, "Query", "findUsers")
_SAVE_PROFILE = Resolver(save_profile, "Mutation", "saveProfile")


def _user() -> User:
    """Create a user with values of each encoded kind."""
    return User(
        user_id="u1",
        colour=Colour.GREEN,
        score=decimal.Decimal("1.5"),
        data={"count": decimal.Decimal(3), "ratio": decimal.Decimal("0.25")},
        created=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    )


class DecodeArgumentsTests(unittest.TestCase):
    def test_omitted_arguments_take_defaults_or_none(self):
        kwargs = decode_arguments(_FIND_USERS, {"userId": "u1"}, to_camel)

        # Defaults are left to Python; nullable arguments without one become None
        self.assertEqual(kwargs, {"user_id": "u1", "older_than": None})

    def test_null_takes_the_default_only_if_none_isnt_accepted(self):
        kwargs = decode_arguments(
            _FIND_USERS,
            {"userId": "u1", "olderThan": 30, "limit": None, "name": None},
            to_camel,
        )

        self.assertEqual(kwargs, {"user_id": "u1", "older_than": 30, "name": None})

    def test_required_arguments_must_be_given(self):
        with self.assertRaisesRegex(ArgumentError, "'userId' is required"):
            decode_arguments(_FIND_USERS, {}, to_camel)

    def test_unknown_arguments_are_rejected(self):
        with self.assertRaisesRegex(ArgumentError, "Unknown argument.*user_id"):
            decode_arguments(_FIND_USERS, {"user_id": "u1"}, to_camel)

    def test_input_objects_are_renamed_decoded_and_validated(self):
        kwargs = decode_arguments(
            _SAVE_PROFILE,
            {
                "profile": {
                    "userId": "u1",
                    "colour": "RED",
                    "address": {"streetName": "High Street"},
                    "born": "2000-01-02T03:04:05Z",
                    "score": 1.25,
                    "extra": '{"a": [1, 2]}',
                },
                "colours": ["GREEN", "RED"],
            },
            to_camel,
        )

        profile = kwargs["profile"]
        self.assertEqual(profile.user_id, "u1")
        self.assertIs(profile.colour, Colour.RED)
        self.assertEqual(profile.address.street_name, "High Street")
        self.assertEqual(
            profile.born, datetime.datetime(2000, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        )
        self.assertEqual(profile.score, decimal.Decimal("1.25"))
        self.assertEqual(profile.extra, {"a": [1, 2]})
        self.assertEqual(kwargs["colours"], [Colour.GREEN, Colour.RED])

    def test_enum_values_are_member_names(self):
        def paint(colours: list[Colour]) -> str:
            raise NotImplementedError

        with self.assertRaisesRegex(ArgumentError, "'red' isn't a member of Colour"):
            decode_arguments(
                Resolver(paint, "Mutation", "paint"), {"colours": ["red"]}, to_camel
            )

    def test_validation_errors_name_the_argument(self):
        with self.assertRaisesRegex(ArgumentError, "Argument 'olderThan'"):
            decode_arguments(
                _FIND_USERS, {"userId": "u1", "olderThan": "old"}, to_camel
            )


class EncodeResultTests(unittest.TestCase):
    def test_only_selected_fields_are_read(self):
        user = _user()

        result = encode_result(user, User, ["userId", "__typename"], to_camel)

        self.assertEqual(result, {"userId": "u1"})
        self.assertEqual(user.post_count, 0)

    def test_properties_and_nested_selections(self):
        user = _user()

        result = encode_result(
            [user], list[User], ["userId", "posts", "posts/content"], to_camel
        )

        self.assertEqual(
            result,
            [{"userId": "u1", "posts": [{"content": "first"}, {"content": "second"}]}],
        )
        self.assertEqual(user.post_count, 1)

    def test_scalars_are_json(self):
        result = encode_result(
            _user(), User, ["colour", "score", "data", "created"], to_camel
        )

        self.assertEqual(
            result,
            {
                "colour": "GREEN",
                "score": 1.5,
                "data": {"count": 3, "ratio": 0.25},
                "created": "2026-01-02T03:04:05+00:00",
            },
        )

    def test_pages_publish_items_and_token(self):
        page = Page[User](items=[_user()], token={"PK": "USER#u1"})

        result = encode_result(
            page, Page[User], ["items", "items/userId", "token"], to_camel
        )

        self.assertEqual(
            result, {"items": [{"userId": "u1"}], "token": {"PK": "USER#u1"}}
        )

    def test_none_is_kept(self):
        self.assertIsNone(encode_result(None, User | None, ["userId"], to_camel))

    def test_wrong_model_type_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "Expected User, got Post"):
            encode_result(Post(content="x"), User, ["userId"], to_camel)


if __name__ == "__main__":
    unittest.main()
