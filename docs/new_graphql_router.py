"""An AppSync API for users and their posts, resolved by one Lambda function.

Deploy `router.sdl()` as the API's schema, then attach this function as a direct Lambda
resolver to each `Query` and `Mutation` field. Other fields, such as `User.posts`, are
resolved from what the resolvers return.
"""

import os

import boto3

from pynamo.dynamo import DynamoModel, Page, gt, single_table_indexes
from pynamo.new_graphql_api import ID, AppSyncEvent, Router

# Every model reads and writes this table unless given another
DynamoModel.default_table(boto3.resource("dynamodb").Table(os.environ["DYNAMO_TABLE"]))


class Post(DynamoModel):
    """A post written by a user."""

    indexes = single_table_indexes(base=("USER#{user_id}", "POST#{post_id}"))

    user_id: ID
    post_id: ID
    content: str


class User(DynamoModel):
    """A user of the app."""

    indexes = single_table_indexes(
        base=("USER#{user_id}", "USER"),
        # Puts every user in one partition, so they can be listed with a query
        global_indexes=[("USERS", "USER#{user_id}")],
    )

    user_id: ID
    name: str
    age: int

    @property
    def posts(self) -> list[Post]:
        """The user's posts; only queried when a request selects them."""
        pages = Post.query({"user_id": self.user_id})
        return [post for page in pages for post in page.items]


router = Router()


@router.query()
def get_user(user_id: ID) -> User:
    """Get a user by ID."""
    return User.get(user_id=user_id)


@router.query()
def list_users(
    older_than: int | None = None, limit: int = 20, token: dict | None = None
) -> Page[User]:
    """List users a page at a time, optionally only those older than an age."""
    values = {} if older_than is None else {"age": gt(older_than)}
    return next(User.query(values, "GSI1", limit=limit, start_token=token))


@router.mutation()
def update_user(user_id: ID, name: str | None = None, age: int | None = None) -> User:
    """Update a user's name or age; arguments left out are left unchanged."""
    changes = {"name": name, "age": age}
    updates = {key: value for key, value in changes.items() if value is not None}
    return User.update(user_id=user_id, **updates)


@router.query()
def me(event: AppSyncEvent) -> User:
    """Get the signed-in user, identified by their Cognito user pool `sub`."""
    identity = event["identity"]
    if identity is None or "sub" not in identity:
        raise PermissionError("Sign in to see your profile")
    return User.get(user_id=identity["sub"])


def lambda_handler(event: AppSyncEvent, context: object) -> object:
    """Resolve the field AppSync invoked this function for."""
    return router(event, context)


if __name__ == "__main__":
    # Print the schema to deploy, e.g. `python new_graphql_router.py > schema.graphql`
    print(router.sdl())
