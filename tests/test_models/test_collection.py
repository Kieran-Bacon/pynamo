"""Tests for querying several models at once with `pynamo.models.collection`."""

from typing import ClassVar
from unittest.mock import Mock

import pytest

from pynamo.models import DynamoModel, KeyPair, PrimaryIndex, single_table_indexes
from pynamo.models.collection import DynamoCollection
from pynamo.models.conditions import GreaterThanOrEqual


class User(DynamoModel):
    """A user's profile, stored in the user's partition."""

    user_id: str
    name: str

    class Meta:
        indexes = single_table_indexes(
            base=("USER#{user_id}", "PROFILE"),
            global_indexes=[("NAME#{name}", "USER")],
        )


class Post(DynamoModel):
    """A user's post, stored in the user's partition."""

    user_id: str
    post_id: str
    title: str

    class Meta:
        indexes = single_table_indexes(base=("USER#{user_id}", "POST#{post_id}"))


class Account(DynamoModel):
    """An account, whose partition is keyed on a differently named attribute."""

    owner_id: str

    class Meta:
        indexes = single_table_indexes(base=("USER#{owner_id}", "ACCOUNT"))


USER_ITEM = {"PK": "USER#u1", "SK": "PROFILE", "user_id": "u1", "name": "Ann"}
"""A stored `User`."""

POST_ITEM = {
    "PK": "USER#u1",
    "SK": "POST#p1",
    "user_id": "u1",
    "post_id": "p1",
    "title": "Hi",
}
"""A stored `Post`."""


# Construction


def test_collections_need_distinct_models():
    with pytest.raises(ValueError, match="at least one model"):
        DynamoCollection()
    with pytest.raises(ValueError, match="more than once"):
        DynamoCollection(User, User)


def test_collections_reject_overlapping_key_prefixes():
    class Draft(DynamoModel):
        """A draft whose sort key starts with the same text as a post's."""

        user_id: str
        draft_id: str

        class Meta:
            indexes = single_table_indexes(
                base=("USER#{user_id}", "POST#DRAFT#{draft_id}")
            )

    with pytest.raises(ValueError, match="Post and Draft have overlapping"):
        DynamoCollection(Post, Draft)


def test_collections_need_keys_stored_under_the_same_attributes():
    class Legacy(DynamoModel):
        """A model whose primary key attributes aren't named PK and SK."""

        user_id: str

        class Meta:
            indexes: ClassVar[list] = [
                PrimaryIndex(KeyPair("id", "USER#{user_id}"), KeyPair("SK", "LEGACY"))
            ]

    with pytest.raises(ValueError, match="same attributes"):
        DynamoCollection(User, Legacy)


# Query


def test_query_returns_each_item_as_its_model(table, render):
    table.query.return_value = {"Items": [USER_ITEM, POST_ITEM]}

    (page,) = DynamoCollection(User, Post).query({"user_id": "u1"}, table=table)
    request = table.query.call_args.kwargs

    assert render(request["KeyConditionExpression"], is_key_condition=True) == (
        "PK = 'USER#u1'"
    )
    assert render(request["FilterExpression"]) == (
        "((begins_with(PK, 'USER#') AND begins_with(SK, 'PROFILE')) OR "
        "(begins_with(PK, 'USER#') AND begins_with(SK, 'POST#')))"
    )
    assert [type(model) for model in page.items] == [User, Post]
    assert page.items[1].model_dump() == {
        "user_id": "u1",
        "post_id": "p1",
        "title": "Hi",
    }
    assert all(model._table is table for model in page.items)


def test_query_collects_items_that_fail_validation(table):
    table.query.return_value = {"Items": [USER_ITEM, POST_ITEM | {"title": 1}]}

    (page,) = DynamoCollection(User, Post).query({"user_id": "u1"}, table=table)

    assert [type(model) for model in page.items] == [User]
    assert page.errors[0].item["SK"] == "POST#p1"


def test_pages_are_read_by_model(table):
    second_post = POST_ITEM | {"SK": "POST#p2", "post_id": "p2"}
    table.query.return_value = {"Items": [USER_ITEM, POST_ITEM, second_post]}

    (page,) = DynamoCollection(User, Post).query({"user_id": "u1"}, table=table)

    # Each model's items keep the order DynamoDB returned them in
    assert [user.name for user in page[User]] == ["Ann"]
    assert [post.post_id for post in page[Post]] == ["p1", "p2"]
    assert len(page.items) == 3


def test_pages_have_items_for_every_model_in_the_collection_only(table):
    table.query.return_value = {"Items": [USER_ITEM]}

    (page,) = DynamoCollection(User, Post).query({"user_id": "u1"}, table=table)

    assert page[Post] == []
    with pytest.raises(KeyError, match="Account isn't in the collection"):
        page[Account]


def test_query_matches_partition_keys_built_from_different_attributes(table):
    models = DynamoCollection(User, Account)

    with pytest.raises(ValueError, match="missing attribute owner_id"):
        next(models.query({"user_id": "u1"}, table=table))
    with pytest.raises(ValueError, match="different partition keys"):
        next(models.query({"user_id": "u1", "owner_id": "u2"}, table=table))

    next(models.query({"user_id": "u1", "owner_id": "u1"}, table=table))
    assert table.query.called


def test_query_only_takes_partition_key_values(table):
    models = DynamoCollection(User, Post)

    with pytest.raises(TypeError, match="not: post_id"):
        next(models.query({"user_id": "u1", "post_id": "p1"}, table=table))
    with pytest.raises(ValueError, match="plain values"):
        next(models.query({"user_id": GreaterThanOrEqual("u1")}, table=table))


def test_query_needs_every_model_to_have_the_index(table):
    with pytest.raises(ValueError, match="Post.Meta has no index named 'GSI1'"):
        next(DynamoCollection(User, Post).query({"name": "Ann"}, "GSI1", table=table))


def test_query_uses_the_models_shared_default_table(table):
    class Base(DynamoModel):
        """A base model for the default table, so other tests' models are unaffected."""

        id: str

        class Meta:
            indexes = single_table_indexes(base=("BASE#{id}", "BASE"))

    class Profile(Base):
        """A profile inheriting the default table."""

        user_id: str

        # Each model declares its own Meta; ty reports redeclaring it in a subclass
        class Meta:  # ty: ignore[invalid-attribute-override]
            indexes = single_table_indexes(base=("USER#{user_id}", "PROFILE"))

    class Note(Base):
        """A note inheriting the default table."""

        user_id: str
        note_id: str

        # Each model declares its own Meta; ty reports redeclaring it in a subclass
        class Meta:  # ty: ignore[invalid-attribute-override]
            indexes = single_table_indexes(base=("USER#{user_id}", "NOTE#{note_id}"))

    Base.default_table(table)
    next(DynamoCollection(Profile, Note).query({"user_id": "u1"}))
    assert table.query.called

    Note.default_table(Mock())
    with pytest.raises(ValueError, match="different default tables"):
        next(DynamoCollection(Profile, Note).query({"user_id": "u1"}))
