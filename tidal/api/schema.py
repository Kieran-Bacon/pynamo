"""Build the GraphQL schema an AppSync API is deployed with from registered resolvers.

The schema is only needed to deploy the API; resolving events doesn't use it. Python
annotations map to GraphQL types as follows:

- `str`, `int`, `float` and `bool` map to the built-in scalars, and `Decimal` to
  `Float`.
- `datetime`, `date` and `time` map to `AWSDateTime`, `AWSDate` and `AWSTime`, and
  `dict` to `AWSJSON`.
- `list`, `set` and `frozenset` map to lists.
- Enums map to enums of their member names.
- Pydantic models map to object types of their fields and their public, annotated
  properties. Used as an argument, a model maps to an input type of its fields, named
  with an `Input` suffix. A generic model is named after its arguments and then itself,
  e.g. `Page[User]` is `UserPage`, and `Page` exposes only its `items` and `token`.
- `X | None` makes a type nullable. Every other type is non-null, though an argument
  with a default is optional.

Field and argument names go through the router's name converter; type and enum value
names are kept as they are.
"""

import datetime
import decimal
import enum
import inspect
import typing
from collections.abc import Callable, Iterable, Mapping
from types import UnionType
from typing import Annotated, Any, NamedTuple, TypeAliasType

import graphql
import pydantic
from graphql import (
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLEnumType,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLFloat,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLNullableType,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    GraphQLType,
)

from ..models import Page
from .errors import SchemaError
from .resolvers import Operation, Resolver

type NameConverter = Callable[[str], str]
"""Converts a Python field, argument or resolver name to its GraphQL name."""

# AppSync defines these scalars itself and rejects schemas that declare them
_AWS_JSON = GraphQLScalarType("AWSJSON")
_AWS_DATE_TIME = GraphQLScalarType("AWSDateTime")
_AWS_DATE = GraphQLScalarType("AWSDate")
_AWS_TIME = GraphQLScalarType("AWSTime")
_AWS_SCALAR_DEFINITIONS = frozenset(
    f"scalar {scalar.name}"
    for scalar in (_AWS_JSON, _AWS_DATE_TIME, _AWS_DATE, _AWS_TIME)
)

# The scalar each Python type maps to, matched exactly (so `bool` isn't an `int`)
_SCALARS: dict[Any, GraphQLScalarType] = {
    str: GraphQLString,
    int: GraphQLInt,
    float: GraphQLFloat,
    bool: GraphQLBoolean,
    decimal.Decimal: GraphQLFloat,
    dict: _AWS_JSON,
    datetime.datetime: _AWS_DATE_TIME,
    datetime.date: _AWS_DATE,
    datetime.time: _AWS_TIME,
}

type _NamedType = (
    GraphQLScalarType | GraphQLEnumType | GraphQLObjectType | GraphQLInputObjectType
)
"""A named GraphQL type this module creates or uses."""

# The only `Page` fields published; `errors` holds raw items and validation errors
_PAGE_FIELDS = ("items", "token")


class ModelField(NamedTuple):
    """A field a model exposes in the schema.

    Attributes:
        annotation: The field's type annotation
        description: The field's description, or None
    """

    annotation: Any
    description: str | None


def _description(doc: str | None) -> str | None:
    """Clean a docstring's indentation for use as a description, keeping None as None."""
    if doc is None:
        return None
    return inspect.cleandoc(doc)


def unwrap_annotation(annotation: Any) -> tuple[Any, bool]:
    """Strip `Annotated`, type aliases and `None` from an annotation.

    Returns:
        The underlying annotation, and whether the annotation accepts None

    Raises:
        SchemaError: If the annotation is a union of more than one type besides None
    """
    nullable = False
    while True:
        if typing.get_origin(annotation) is Annotated:
            # Metadata such as pydantic constraints doesn't affect the GraphQL type
            annotation = typing.get_args(annotation)[0]
        elif isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif typing.get_origin(annotation) is UnionType:
            members = typing.get_args(annotation)
            types = [member for member in members if member is not type(None)]
            if len(types) != 1:
                raise SchemaError(f"union types aren't supported: {annotation}")
            nullable = nullable or len(types) < len(members)
            annotation = types[0]
        else:
            return annotation, nullable


def _type_name(cls: type) -> str:
    """Get a class's GraphQL type name; generic models are named after their arguments.

    Raises:
        SchemaError: If a generic model's argument isn't a class
    """
    metadata = getattr(cls, "__pydantic_generic_metadata__", None)
    if not metadata or metadata["origin"] is None:
        return cls.__name__

    # E.g. Page[User] is UserPage
    names = []
    for argument in metadata["args"]:
        if not isinstance(argument, type):
            raise SchemaError(f"can't name {cls.__name__}: {argument} isn't a class")
        names.append(_type_name(argument))
    return "".join(names) + metadata["origin"].__name__


def _property_fields(model: type[pydantic.BaseModel]) -> dict[str, ModelField]:
    """Get a model's public properties that have a return annotation.

    Raises:
        SchemaError: If a property's return annotation can't be resolved
    """
    fields = {}
    # Visit base classes first so a subclass's property replaces an inherited one
    for cls in reversed(model.__mro__):
        if not issubclass(cls, pydantic.BaseModel) or cls is pydantic.BaseModel:
            continue
        for name, value in vars(cls).items():
            if name.startswith("_") or not isinstance(value, property):
                continue
            if value.fget is None:
                continue
            try:
                hints = typing.get_type_hints(value.fget, include_extras=True)
            except NameError as error:
                raise SchemaError(
                    f"{model.__name__}.{name} has an annotation that can't be resolved: "
                    f"{error}"
                ) from error
            # Unannotated properties are treated as internal and left out
            if "return" in hints:
                fields[name] = ModelField(hints["return"], _description(value.__doc__))
    return fields


def model_fields(
    model: type[pydantic.BaseModel], *, input: bool
) -> dict[str, ModelField]:
    """Get the fields a model exposes in the schema, by Python name.

    Args:
        model: The model to describe
        input: Whether the model is used as an input, which leaves out properties

    Raises:
        SchemaError: If the model or a property has annotations that can't be resolved
    """
    # Models referencing classes defined after them are completed on first use
    if not model.__pydantic_complete__:
        try:
            model.model_rebuild()
        except pydantic.PydanticUndefinedAnnotation as error:
            raise SchemaError(
                f"{model.__name__} has an annotation that can't be resolved: {error}"
            ) from error

    fields = {
        name: ModelField(info.annotation, info.description)
        for name, info in model.model_fields.items()
    }
    if not input:
        fields |= _property_fields(model)

    # A page is published as its items and the token to continue from
    if issubclass(model, Page):
        fields = {name: fields[name] for name in _PAGE_FIELDS}
    return fields


def graphql_names(
    names: Iterable[str], converter: NameConverter, owner: str
) -> dict[str, str]:
    """Convert Python names to GraphQL names, checking that none collide.

    Args:
        names: The Python names of a type's fields or a resolver's arguments
        converter: The router's name converter
        owner: What the names belong to, for the error message

    Returns:
        Each GraphQL name by its Python name

    Raises:
        SchemaError: If two names convert to the same GraphQL name
    """
    converted: dict[str, str] = {}
    for name in names:
        graphql_name = converter(name)
        if graphql_name in converted.values():
            raise SchemaError(f"{owner} has more than one field named {graphql_name!r}")
        converted[name] = graphql_name
    return converted


class _TypeMapper:
    """Maps annotations to GraphQL types, creating each named type only once.

    Attributes:
        _converter: Converts Python field and argument names to GraphQL names
        _types: Each named type created, by its class and whether it's an input
        _owners: The class and input flag each GraphQL type name was created for
    """

    def __init__(self, converter: NameConverter) -> None:
        """Create a mapper that names fields with `converter`."""
        self._converter = converter
        self._types: dict[tuple[type, bool], _NamedType] = {}
        self._owners: dict[str, tuple[type, bool]] = {}

    def _claim(self, name: str, owner: tuple[type, bool]) -> str:
        """Reserve a GraphQL type name for a class.

        Raises:
            SchemaError: If another class already maps to the name
        """
        claimed = self._owners.setdefault(name, owner)
        if claimed != owner:
            raise SchemaError(
                f"{owner[0].__qualname__} and {claimed[0].__qualname__} both map to the "
                f"GraphQL type {name!r}"
            )
        return name

    def _enum_type(self, cls: type[enum.Enum]) -> GraphQLEnumType:
        """Create an enum type whose values are the enum's member names."""
        name = self._claim(cls.__name__, (cls, False))
        values = {member.name: GraphQLEnumValue(member) for member in cls}
        return GraphQLEnumType(name, values, description=_description(cls.__doc__))

    def _model_type(
        self, model: type[pydantic.BaseModel], input: bool
    ) -> GraphQLObjectType | GraphQLInputObjectType:
        """Create an object type, or an input type if `input`, for a model.

        Raises:
            SchemaError: If one of the model's fields has an unsupported type
        """
        name = _type_name(model) + ("Input" if input else "")
        self._claim(name, (model, input))
        description = _description(model.__doc__)

        # Cache the type before mapping its fields, so models can reference themselves
        fields: dict[str, Any] = {}
        if input:
            model_type = GraphQLInputObjectType(name, lambda: fields, description)
        else:
            model_type = GraphQLObjectType(
                name, lambda: fields, description=description
            )
        self._types[(model, input)] = model_type

        # Map each field, saying which field is at fault if one can't be
        exposed = model_fields(model, input=input)
        names = graphql_names(exposed, self._converter, model.__name__)
        for python_name, field in exposed.items():
            try:
                field_type = self.graphql_type(field.annotation, input=input)
            except SchemaError as error:
                raise SchemaError(f"{model.__name__}.{python_name}: {error}") from error
            if input:
                fields[names[python_name]] = GraphQLInputField(
                    graphql.assert_input_type(field_type),
                    description=field.description,
                )
            else:
                fields[names[python_name]] = GraphQLField(
                    graphql.assert_output_type(field_type),
                    description=field.description,
                )
        return model_type

    def _named_type(self, cls: Any, input: bool) -> _NamedType:
        """Get the scalar, enum or model type for an unwrapped annotation.

        Raises:
            SchemaError: If the annotation isn't a supported type
        """
        if cls in _SCALARS:
            return _SCALARS[cls]
        if not isinstance(cls, type):
            raise SchemaError(f"unsupported type: {cls}")

        # Enums are the same as inputs and outputs, so are only created once
        if issubclass(cls, enum.Enum):
            if (cls, False) not in self._types:
                self._types[(cls, False)] = self._enum_type(cls)
            return self._types[(cls, False)]

        if issubclass(cls, pydantic.BaseModel):
            if (cls, input) not in self._types:
                self._model_type(cls, input)
            return self._types[(cls, input)]

        raise SchemaError(f"unsupported type: {cls.__qualname__}")

    def graphql_type(
        self, annotation: Any, *, input: bool, optional: bool = False
    ) -> GraphQLType:
        """Map an annotation to a GraphQL type.

        Args:
            annotation: A field, argument or return type annotation
            input: Whether the type is used as an input, mapping models to input types
            optional: Make the type nullable even if the annotation doesn't accept None

        Raises:
            SchemaError: If the annotation, or a type within it, isn't supported
        """
        annotation, nullable = unwrap_annotation(annotation)
        origin = typing.get_origin(annotation)

        # Collections become lists, typed dicts JSON, and other generics are unsupported
        if origin in (list, set, frozenset):
            (item,) = typing.get_args(annotation)
            mapped: GraphQLNullableType = GraphQLList(
                self.graphql_type(item, input=input)
            )
        elif origin is dict:
            mapped = _AWS_JSON
        elif origin is not None:
            raise SchemaError(f"unsupported type: {annotation}")
        else:
            mapped = self._named_type(annotation, input)

        if nullable or optional:
            return mapped
        return GraphQLNonNull(mapped)


def _root_field(
    mapper: _TypeMapper, resolver: Resolver, converter: NameConverter
) -> GraphQLField:
    """Create the `Query` or `Mutation` field for a resolver.

    Raises:
        SchemaError: If an argument or the return type isn't supported
    """
    arguments = resolver.arguments
    names = graphql_names(arguments, converter, f"Resolver {resolver.name!r}")

    graphql_arguments = {}
    for python_name, argument in arguments.items():
        try:
            # An argument with a default can be left out, whatever its type
            argument_type = mapper.graphql_type(
                argument.annotation, input=True, optional=argument.has_default
            )
        except SchemaError as error:
            raise SchemaError(
                f"Resolver {resolver.name!r} argument {python_name!r}: {error}"
            ) from error
        graphql_arguments[names[python_name]] = GraphQLArgument(
            graphql.assert_input_type(argument_type)
        )

    try:
        return_type = mapper.graphql_type(resolver.return_annotation, input=False)
    except SchemaError as error:
        raise SchemaError(f"Resolver {resolver.name!r} return type: {error}") from error

    return GraphQLField(
        graphql.assert_output_type(return_type),
        args=graphql_arguments,
        description=resolver.description,
        deprecation_reason=resolver.deprecation_reason,
    )


def build_schema(
    resolvers: Iterable[Resolver], converter: NameConverter
) -> GraphQLSchema:
    """Build the schema whose `Query` and `Mutation` fields are the given resolvers.

    Args:
        resolvers: The resolvers, each with its GraphQL name
        converter: Converts Python field and argument names to GraphQL names

    Raises:
        SchemaError: If there are no queries, two resolvers share a name, a type isn't
            supported, or the result isn't a valid GraphQL schema
    """
    mapper = _TypeMapper(converter)

    # Collect each resolver's field under its root type
    roots: Mapping[Operation, dict[str, GraphQLField]] = {"Query": {}, "Mutation": {}}
    for resolver in resolvers:
        fields = roots[resolver.operation]
        if resolver.name in fields:
            raise SchemaError(
                f"More than one resolver is named {resolver.name!r} on {resolver.operation}"
            )
        fields[resolver.name] = _root_field(mapper, resolver, converter)

    # GraphQL, and so AppSync, requires a Query type with at least one field
    if not roots["Query"]:
        raise SchemaError("A schema must have at least one query")

    schema = GraphQLSchema(
        query=GraphQLObjectType("Query", roots["Query"]),
        mutation=GraphQLObjectType("Mutation", roots["Mutation"])
        if roots["Mutation"]
        else None,
    )

    # Catch what the mapping can't, such as a converter producing invalid names
    errors = graphql.validate_schema(schema)
    if errors:
        raise SchemaError("\n".join(error.message for error in errors))
    return schema


def print_sdl(schema: GraphQLSchema) -> str:
    """Print a schema as SDL that AppSync accepts, leaving out AppSync's own scalars."""
    blocks = graphql.print_schema(schema).split("\n\n")
    return "\n\n".join(
        block for block in blocks if block not in _AWS_SCALAR_DEFINITIONS
    )
