# tidal

tidal is a toolkit for building event-driven, serverless applications on DynamoDB and AWS AppSync with pydantic. You describe your data once, as pydantic models, and tidal uses those same models at every stage of a change's life:

1. **Stored**: [`tidal.models`](models.md) reads and writes the models in a DynamoDB table, building every key from the model's own attributes.
2. **Served**: [`tidal.api`](api.md) publishes functions that take and return the models as an AppSync GraphQL API, generating the schema from their type hints.
3. **Reacted to**: [`tidal.streams`](streams.md) hands every change to an item back to your code as a before and after pair of models, so work can follow on from a write without the writer knowing about it.

A change moves through an application like a tide. It comes in through the API, is written to the table, and then flows back out through the stream to whatever reacts to it:

```
client ── GraphQL ──▶ AppSync ──▶ Lambda: tidal.api ──▶ DynamoDB: tidal.models
                                                              │
                                                         item change
                                                              ▼
                       Lambda: tidal.streams ◀───────── Kinesis data stream
```

At every step the data is validated as the same pydantic class, so a field added to a model is stored, published in the schema and delivered to stream handlers without being declared anywhere else.

## Installation

tidal needs Python 3.14 or later.

```
pip install tidal
```

`tidal.api` also needs `graphql-core`, which the `graphql` extra installs:

```
pip install "tidal[graphql]"
```

## A model's life

One model, served through the API and reacted to when it changes:

```python
import logging

import boto3

from tidal.api import Router
from tidal.models import DynamoModel, single_table_indexes
from tidal.streams import DynamoStreamHandler

DynamoModel.default_table(boto3.resource("dynamodb").Table("app"))


# Stored: keys are built from the model's attributes
class User(DynamoModel):
    """A user of the app."""

    indexes = single_table_indexes(base=("USER#{user_id}", "USER"))

    user_id: str
    name: str


# Served: the schema is generated from the resolvers' signatures
router = Router()


@router.query()
def get_user(user_id: str) -> User:
    """Get a user by ID."""
    return User.get(user_id=user_id)


@router.mutation()
def rename_user(user_id: str, name: str) -> User:
    """Change a user's name."""
    return User.update(user_id=user_id, name=name)


# Reacted to: each change arrives as the item before and after it
stream = DynamoStreamHandler()


@stream.handle(User, events=["MODIFY"])
def on_user_changed(old: User | None, new: User | None) -> None:
    """Record renames once they've been written."""
    if old is not None and new is not None and old.name != new.name:
        logging.info("User %s renamed from %s to %s", new.user_id, old.name, new.name)


# Each function is deployed as its own Lambda
def api_handler(event, context):
    return router(event, context)


def stream_handler(event, context):
    return stream.lambda_handler(event, context)
```

`rename_user` doesn't know that anything is listening, and `on_user_changed` doesn't know where the change came from. A rename made by an admin script, a migration or another service reaches the same handler.

## Using the parts on their own

The three subpackages share models but don't depend on each other:

- `tidal.models` is a complete DynamoDB ORM without the API or streams.
- `tidal.api` serves any pydantic model, not just `DynamoModel`s, so it can front data stored elsewhere.
- `tidal.streams` validates change images as any pydantic model, so it can consume changes from a table written by other code.

## Guides

- [Models](models.md): defining models and their keys, and reading and writing them with `get`, `query`, `scan`, `put`, `update` and `delete`.
- [GraphQL API](api.md): registering resolvers, how Python types map to GraphQL, and deploying the router to AppSync.
- [Streams](streams.md): registering change handlers, how a change finds its handler, and how failures and timeouts are retried.
- [Considerations](CONSIDERATIONS.md): the limitations and quirks of each part.

[`new_graphql_router.py`](new_graphql_router.py) is a complete AppSync API for users and their posts.
