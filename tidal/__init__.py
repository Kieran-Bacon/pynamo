"""Event-driven applications on DynamoDB and AWS AppSync, built from pydantic models.

A model is defined once and used at every stage of a change's life:

- `tidal.models` stores models in a DynamoDB table as `DynamoModel`s.
- `tidal.api` serves them through AppSync, with a `Router` as a direct Lambda resolver.
- `tidal.streams` reacts to their changes, delivered through Kinesis, with a
  `DynamoStreamHandler`.

Each subpackage is imported on its own; `tidal.api` needs the `graphql` extra.
"""
