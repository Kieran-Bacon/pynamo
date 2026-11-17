"""The `Router` that registers resolvers and dispatches AppSync events to them.

Functions are registered as fields of `Query` or `Mutation` with decorators, and the
router is then used as the Lambda handler:

    router = Router()

    @router.query()
    def get_user(user_id: str) -> User: ...

    def lambda_handler(event, context):
        return router(event, context)

Each event names the field it resolves, so it's dispatched straight to that field's
resolver; the GraphQL query isn't parsed or executed again. `sdl()` prints the schema
to deploy the AppSync API with.
"""

import inspect
from types import FunctionType
from typing import Any, Protocol

from graphql import GraphQLSchema
from pydantic.alias_generators import to_camel

from .codec import decode_arguments, encode_result
from .errors import ResolverNotFoundError, SchemaError
from .events import AppSyncEvent
from .resolvers import Operation, Resolver
from .schema import NameConverter, build_schema, print_sdl


class _Decorator(Protocol):
    """A decorator that registers a function and returns it unchanged."""

    def __call__[F: FunctionType](self, func: F, /) -> F:
        """Register `func` and return it."""
        ...


class Router:
    """Registers resolvers for an AppSync API and resolves its Lambda events.

    Attributes:
        name_converter: Converts Python names to GraphQL field, argument and resolver
            names
        _resolvers: Each resolver, by its root type and GraphQL name
        _schema: The schema built from the resolvers, until another is registered
    """

    def __init__(self, *, name_converter: NameConverter | None = to_camel) -> None:
        """Create a router.

        Args:
            name_converter: Converts Python names to GraphQL names; camelCase by
                default, or None to keep Python names
        """
        # `str` returns a string unchanged, so it keeps the Python names
        self.name_converter: NameConverter = name_converter or str
        self._resolvers: dict[tuple[str, str], Resolver] = {}
        self._schema: GraphQLSchema | None = None

    def _add(self, resolver: Resolver) -> None:
        """Add a resolver, dropping the cached schema.

        Raises:
            SchemaError: If a resolver with the same name is already registered
        """
        key = (resolver.operation, resolver.name)
        if key in self._resolvers:
            raise SchemaError(
                f"More than one resolver is named {resolver.name!r} on "
                f"{resolver.operation}"
            )
        self._resolvers[key] = resolver
        self._schema = None

    def _register(
        self,
        operation: Operation,
        name: str | None,
        description: str | None,
        deprecation_reason: str | None,
    ) -> _Decorator:
        """Create a decorator registering a function as a field of `operation`."""

        def register[F: FunctionType](func: F) -> F:
            """Register `func` as a resolver and return it unchanged.

            Raises:
                SchemaError: If `func` is a coroutine function or its name is taken
            """
            # Resolvers are called synchronously, so a coroutine would never run
            if inspect.iscoroutinefunction(func):
                raise SchemaError(f"Resolver {func.__name__!r} must not be async")

            self._add(
                Resolver(
                    func=func,
                    operation=operation,
                    name=name or self.name_converter(func.__name__),
                    description=description or inspect.getdoc(func),
                    deprecation_reason=deprecation_reason,
                )
            )
            return func

        return register

    def query(
        self,
        name: str | None = None,
        *,
        description: str | None = None,
        deprecation_reason: str | None = None,
    ) -> _Decorator:
        """Register a function as a field of `Query`.

        Args:
            name: The field's GraphQL name; defaults to the converted function name
            description: The field's description; defaults to the function's docstring
            deprecation_reason: Marks the field deprecated, giving the reason
        """
        return self._register("Query", name, description, deprecation_reason)

    def mutation(
        self,
        name: str | None = None,
        *,
        description: str | None = None,
        deprecation_reason: str | None = None,
    ) -> _Decorator:
        """Register a function as a field of `Mutation`.

        Args:
            name: The field's GraphQL name; defaults to the converted function name
            description: The field's description; defaults to the function's docstring
            deprecation_reason: Marks the field deprecated, giving the reason
        """
        return self._register("Mutation", name, description, deprecation_reason)

    def include_router(self, router: Router) -> None:
        """Add another router's resolvers to this one.

        The resolvers are copied, so ones registered on `router` afterwards aren't
        included.

        Raises:
            SchemaError: If the routers convert names differently, or a resolver's
                name is already taken
        """
        # Resolver names were converted when registered, so must match this router's
        if router.name_converter is not self.name_converter:
            raise SchemaError("Included routers must use the same name converter")
        for resolver in router._resolvers.values():
            self._add(resolver)

    def schema(self) -> GraphQLSchema:
        """Build the GraphQL schema of the registered resolvers, reusing it until changed.

        Raises:
            SchemaError: If a resolver can't be represented in the schema
        """
        if self._schema is None:
            self._schema = build_schema(self._resolvers.values(), self.name_converter)
        return self._schema

    def sdl(self) -> str:
        """Print the schema as SDL to deploy the AppSync API with.

        Raises:
            SchemaError: If a resolver can't be represented in the schema
        """
        schema = self.schema()
        return print_sdl(schema)

    def __call__(self, event: AppSyncEvent, context: object = None) -> Any:
        """Resolve an AppSync event; usable as the Lambda handler.

        Args:
            event: The event AppSync invoked the function with
            context: The Lambda context, which isn't used

        Returns:
            The field's value, as JSON for AppSync

        Raises:
            ResolverNotFoundError: If no resolver is registered for the event's field
            ArgumentError: If the event's arguments don't match the resolver
        """
        info = event["info"]
        resolver = self._resolvers.get((info["parentTypeName"], info["fieldName"]))
        if resolver is None:
            raise ResolverNotFoundError(
                f"No resolver is registered for "
                f"{info['parentTypeName']}.{info['fieldName']}"
            )

        # Decode the arguments, passing the event to a parameter that asks for it
        kwargs = decode_arguments(
            resolver, event.get("arguments") or {}, self.name_converter
        )
        if resolver.event_parameter is not None:
            kwargs[resolver.event_parameter] = event

        result = resolver.func(**kwargs)
        return encode_result(
            result,
            resolver.return_annotation,
            info["selectionSetList"],
            self.name_converter,
        )
