# pynamo documentation

pynamo provides pydantic models and utilities for DynamoDB.

- [Dynamo models](dynamo.md): defining models and their indexes, then reading them with
  `get`, `query` and `scan`, and the limits to be aware of.
- [GraphQL API](graphql.md): serving models through AWS AppSync with a Lambda resolver,
  using `pynamo.graphql_api`.
- [Considerations](CONSIDERATIONS.md): the limitations and quirks of the implementation.
