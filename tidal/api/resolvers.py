"""Functions registered as fields of the `Query` and `Mutation` types.

A `Resolver` records a function and how it appears in the schema. Its parameters are
the field's GraphQL arguments, except one annotated `AppSyncEvent`, which is passed the
invoking event instead. Annotations are resolved on first use rather than when the
function is registered, so they can reference classes defined after it.
"""

import functools
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from .errors import SchemaError
from .events import AppSyncEvent

type Operation = Literal["Query", "Mutation"]
"""The root type a resolver is a field of."""


class Argument(NamedTuple):
    """A resolver parameter exposed as a GraphQL argument.

    Attributes:
        annotation: The parameter's resolved type annotation
        has_default: Whether the parameter has a default, making the argument optional
    """

    annotation: Any
    has_default: bool


@dataclass(frozen=True)
class Resolver:
    """A function resolving a field of `Query` or `Mutation`.

    Attributes:
        func: The function called to resolve the field
        operation: The root type the field belongs to
        name: The field's GraphQL name
        description: The field's description in the schema
        deprecation_reason: Why the field is deprecated, or None if it isn't
    """

    func: Callable[..., Any]
    operation: Operation
    name: str
    description: str | None = None
    deprecation_reason: str | None = None

    @functools.cached_property
    def _hints(self) -> dict[str, Any]:
        """Resolve the function's annotations, keeping `Annotated` metadata.

        Raises:
            SchemaError: If an annotation references a name that isn't defined
        """
        try:
            return typing.get_type_hints(self.func, include_extras=True)
        except NameError as error:
            raise SchemaError(
                f"Resolver {self.name!r} has an annotation that can't be resolved: {error}"
            ) from error

    @functools.cached_property
    def _parameters(self) -> dict[str, inspect.Parameter]:
        """Get the function's parameters, which must all be passable by keyword.

        Raises:
            SchemaError: If a parameter is positional-only, `*args` or `**kwargs`
        """
        parameters = dict(inspect.signature(self.func).parameters)
        for parameter in parameters.values():
            # Arguments are passed by name, so each parameter must accept a keyword
            if parameter.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise SchemaError(
                    f"Resolver {self.name!r} parameter {parameter.name!r} must be "
                    "passable by keyword"
                )
        return parameters

    @functools.cached_property
    def event_parameter(self) -> str | None:
        """The name of the parameter that is passed the `AppSyncEvent`, if any.

        Raises:
            SchemaError: If more than one parameter is annotated `AppSyncEvent`
        """
        names = [
            name for name in self._parameters if self._hints.get(name) is AppSyncEvent
        ]
        if len(names) > 1:
            raise SchemaError(
                f"Resolver {self.name!r} has more than one AppSyncEvent parameter"
            )
        return names[0] if names else None

    @functools.cached_property
    def arguments(self) -> dict[str, Argument]:
        """Each parameter exposed as a GraphQL argument, by its Python name.

        Raises:
            SchemaError: If a parameter has no annotation, or can't be passed by keyword
        """
        arguments = {}
        for name, parameter in self._parameters.items():
            # The event parameter is supplied by the router, not the caller
            if name == self.event_parameter:
                continue
            if name not in self._hints:
                raise SchemaError(
                    f"Resolver {self.name!r} parameter {name!r} has no type annotation"
                )
            arguments[name] = Argument(
                annotation=self._hints[name],
                has_default=parameter.default is not inspect.Parameter.empty,
            )
        return arguments

    @functools.cached_property
    def return_annotation(self) -> Any:
        """The function's resolved return annotation.

        Raises:
            SchemaError: If the function has no return annotation, or returns None
        """
        annotation = self._hints.get("return", inspect.Parameter.empty)
        # A GraphQL field must have a type, so the return type is required
        if annotation is inspect.Parameter.empty or annotation is type(None):
            raise SchemaError(
                f"Resolver {self.name!r} must annotate the type it returns"
            )
        return annotation
