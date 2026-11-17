"""An example GraphQL router backed by `DynamoModel`s, which would be connected to AppSync.

The table is created once and set as the default for every model, so resolvers don't
need to pass it around.
"""

import os

import boto3

from pynamo.dynamo import DynamoModel, Page, conditions, increment, single_table_indexes
from pynamo.graphql_api import Router

# Set the table every model uses before anything reads or writes
dynamo_table = boto3.resource("dynamodb").Table(os.environ["DYNAMO_TABLE"])
DynamoModel.default_table(dynamo_table)


class Post(DynamoModel):
    indexes = single_table_indexes(base=("USER#{user_id}", "POST#{post_id}"))

    user_id: str
    post_id: str
    content: str
    likes: int = 0


class User(DynamoModel):
    indexes = single_table_indexes(base=("USER#{user_id}", "USER"))

    user_id: str
    name: str
    age: int

    @property
    def posts(self):
        """Get the user's posts, a page at a time, from the table the user came from."""
        # The posts share the user's partition, so the primary index finds them
        return Post.query({"user_id": self.user_id}, table=self._table)


router = Router()
# router.include_router(post_router)  # Example of including another router


@router.query()
def get_user_info(user_id: str) -> User:
    return User.get(user_id=user_id)


@router.query()
def get_users(
    user_id: str, older_than: int | None, continuation_token: dict | None
) -> Page[User]:
    # Only filter on age when asked to
    values = {"user_id": user_id}
    if older_than is not None:
        values["age"] = conditions.gt(older_than)
    return next(User.query(values, start_token=continuation_token))


@router.mutation()
def update_user(user_id: str, name: str | None, age: int | None) -> User:
    # Arguments the client left out are None, and leave the attribute unchanged
    updates = {
        field: value
        for field, value in {"name": name, "age": age}.items()
        if value is not None
    }
    return User.update(user_id=user_id, **updates)


@router.mutation()
def like_post(user_id: str, post_id: str) -> Post:
    return Post.update(user_id=user_id, post_id=post_id, likes=increment(1))
