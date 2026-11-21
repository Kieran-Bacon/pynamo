# GraphQL API

`tidal.api` is how a tidal application's data reaches clients. It serves pydantic models, including [`DynamoModel`s](models.md), through an AWS AppSync API, using one Lambda function as a direct resolver. Python functions are registered on a `Router`, which generates the API's schema from their signatures and resolves the events AppSync sends.

Resolvers only need to read and write models. Follow-on work, such as notifying other users or updating aggregates, can be left to [stream handlers](streams.md), which receive every change a mutation makes once it has been written.

It needs the `graphql` extra, which installs `graphql-core`:

```
pip install "tidal[graphql]"
```

[`new_graphql_router.py`](new_graphql_router.py) is a complete example.

## Registering resolvers

Decorate functions with `router.query()` or `router.mutation()` to make them fields of
`Query` or `Mutation`. Their parameters become the field's arguments and their return
annotation its type:

```python
from tidal.api import Router

router = Router()


@router.query()
def get_user(user_id: str) -> User:
    """Get a user by ID."""
    return User.get({"user_id": user_id})


@router.mutation(deprecation_reason="Use renameUser")
def update_user(user_id: str, name: str | None = None) -> User: ...
```

This publishes:

```graphql
type Query {
  """Get a user by ID."""
  getUser(userId: String!): User!
}

type Mutation {
  updateUser(userId: String!, name: String): User! @deprecated(reason: "Use renameUser")
}
```

Each decorator takes:

- `name`: the field's name, by default the function's name converted to camelCase.
- `description`: by default the function's docstring.
- `deprecation_reason`: marks the field deprecated.

Decorators return the function unchanged, so it can still be called directly, e.g. in
tests. Resolvers must be plain functions; `async def` functions are rejected.

## Names

Field, argument and resolver names are converted from Python's snake_case to camelCase.
Pass a different `name_converter` to `Router` to change this, or `None` to keep Python
names. Type names stay the class names, and enum values stay the member names.

Field aliases aren't used. A `DynamoModel` field aliased to a stored attribute name is
published under its converted Python name.

## Types

| Python | GraphQL |
| --- | --- |
| `str`, `int`, `float`, `bool` | `String`, `Int`, `Float`, `Boolean` |
| `Decimal` | `Float` |
| `datetime`, `date`, `time` | `AWSDateTime`, `AWSDate`, `AWSTime` |
| `dict`, `dict[K, V]` | `AWSJSON` |
| `list[T]`, `set[T]`, `frozenset[T]` | `[T]` |
| an `Enum` | an enum of its member names |
| a pydantic model | an object type, or an input type named `<Model>Input` when used as an argument |
| `Page[T]` | `<T>Page`, with `items` and `token` |
| `T \| None` | nullable `T`; everything else is non-null |

`Annotated` metadata and `type` aliases are looked through. Other types, including
unions of several types, raise `SchemaError`.

A model's object type has its fields and its public properties that have a return
annotation. Properties are only read when a request selects them, so a property can
query related items:

```python
class User(DynamoModel):
    ...

    @property
    def posts(self) -> list[Post]:
        """The user's posts; only queried when a request selects them."""
        pages = Post.query({"user_id": self.user_id})
        return [post for page in pages for post in page.items]
```

Input types only have the model's fields.

## Arguments

Arguments are converted to Python and validated with pydantic before the resolver is
called. Enum names become members, input objects become models, and `AWSJSON` text is
parsed. Invalid arguments raise `ArgumentError`.

An argument is optional if its parameter has a default or accepts `None`:

- Left out, it takes the default, or `None` if there is no default.
- Given as `null`, it's `None` if the parameter accepts `None`, and otherwise takes the
  default.

## The event

A parameter annotated `AppSyncEvent` is passed the event AppSync sent, rather than
being an argument. Use it to read the caller's `identity` or the request headers:

```python
@router.query()
def me(event: AppSyncEvent) -> User:
    identity = event["identity"]
    if identity is None or "sub" not in identity:
        raise PermissionError("Sign in to see your profile")
    return User.get({"user_id": identity["sub"]})
```

## Combining routers

Routers can be split across modules and combined with `include_router`, which copies
the other router's resolvers. Names must not clash, and both routers must use the same
name converter.

```python
router = Router()
router.include_router(users.router)
router.include_router(posts.router)
```

## Deploying

`router.sdl()` returns the schema to deploy the AppSync API with. It leaves out
AppSync's own scalars, which AppSync doesn't allow a schema to declare. Building it
also checks every resolver, so calling it in a test catches mistakes before they reach
a request.

Use the router as the Lambda handler, and attach the function as a direct Lambda
resolver to each `Query` and `Mutation` field:

```python
def lambda_handler(event, context):
    return router(event, context)
```

Each event is dispatched straight to the resolver for its field; the GraphQL query
isn't parsed again. Only `Query` and `Mutation` fields are resolved: other fields are
read from what those resolvers return.

## Errors

- `SchemaError`: a resolver can't be published, e.g. an unsupported type or a missing
  return annotation. Raised when registering or building the schema.
- `ResolverNotFoundError`: an event names a field with no resolver.
- `ArgumentError`: an event's arguments are missing or invalid.

Exceptions raised by resolvers aren't caught. AppSync reports any exception to the
client with its class name as the `errorType`.

See [Considerations](CONSIDERATIONS.md) for the limitations to be aware of.
