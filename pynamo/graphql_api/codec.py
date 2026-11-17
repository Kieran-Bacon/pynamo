"""Convert between AppSync's JSON values and the Python values resolvers work with.

`decode_arguments` turns an event's arguments into keyword arguments for a resolver:
GraphQL names become Python names, enum names become members, `AWSJSON` text is parsed,
and each value is then validated against its parameter's annotation with pydantic.

`encode_result` turns what a resolver returns into JSON for AppSync, following the
same type mapping as the schema. Only the fields the request selected are read, so a
property such as `User.posts` only runs when a client asks for it.
"""

import datetime
import decimal
import enum
import functools
import json
import typing
from collections.abc import Mapping
from typing import Any, NamedTuple

import pydantic
import pydantic_core

from .errors import ArgumentError
from .resolvers import Argument, Resolver
from .schema import NameConverter, graphql_names, model_fields, unwrap_annotation

type Selection = dict[str, Selection]
"""The fields selected from a value, each with the fields selected from it in turn."""


class _Field(NamedTuple):
    """A model field as it's named in Python, and its annotation."""

    python_name: str
    annotation: Any


class _ArgumentSpec(NamedTuple):
    """How to decode one of a resolver's arguments.

    Attributes:
        python_name: The parameter the argument is passed as
        argument: The parameter's annotation and whether it has a default
        nullable: Whether the parameter's annotation accepts None
        adapter: Validates the decoded value against the parameter's annotation
    """

    python_name: str
    argument: Argument
    nullable: bool
    adapter: pydantic.TypeAdapter[Any]


@functools.cache
def _fields(
    model: type[pydantic.BaseModel], converter: NameConverter, input: bool
) -> dict[str, _Field]:
    """Get a model's exposed fields by GraphQL name, as the schema publishes them."""
    exposed = model_fields(model, input=input)
    names = graphql_names(exposed, converter, model.__name__)
    return {
        names[name]: _Field(name, field.annotation) for name, field in exposed.items()
    }


def _decode(value: Any, annotation: Any, converter: NameConverter) -> Any:
    """Convert a GraphQL input value into data pydantic can validate as `annotation`.

    Raises:
        ArgumentError: If the value doesn't have the shape the annotation requires
    """
    if value is None:
        return None
    annotation, _ = unwrap_annotation(annotation)
    origin = typing.get_origin(annotation)

    # Lists are decoded item by item
    if origin in (list, set, frozenset):
        if not isinstance(value, list):
            raise ArgumentError(f"expected a list, got {value!r}")
        (item,) = typing.get_args(annotation)
        return [_decode(entry, item, converter) for entry in value]

    # AWSJSON may arrive as JSON text rather than parsed
    if annotation is dict or origin is dict:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ArgumentError(f"invalid JSON: {error}") from error

    if not isinstance(annotation, type):
        return value

    # GraphQL enum values are member names, which pydantic would read as values
    if issubclass(annotation, enum.Enum):
        try:
            return annotation[value]
        except KeyError as error:
            raise ArgumentError(
                f"{value!r} isn't a member of {annotation.__name__}"
            ) from error

    # Input objects are renamed to Python field names, decoding each field in turn
    if issubclass(annotation, pydantic.BaseModel):
        if not isinstance(value, Mapping):
            raise ArgumentError(f"expected an object, got {value!r}")
        fields = _fields(annotation, converter, True)
        decoded = {}
        for name, entry in value.items():
            if name not in fields:
                raise ArgumentError(f"{annotation.__name__} has no field {name!r}")
            field = fields[name]
            decoded[field.python_name] = _decode(entry, field.annotation, converter)
        return decoded

    return value


@functools.cache
def _argument_specs(
    resolver: Resolver, converter: NameConverter
) -> dict[str, _ArgumentSpec]:
    """Describe how to decode each of a resolver's arguments, by GraphQL name."""
    arguments = resolver.arguments
    names = graphql_names(arguments, converter, f"Resolver {resolver.name!r}")
    specs = {}
    for python_name, argument in arguments.items():
        _, nullable = unwrap_annotation(argument.annotation)
        specs[names[python_name]] = _ArgumentSpec(
            python_name=python_name,
            argument=argument,
            nullable=nullable,
            adapter=pydantic.TypeAdapter(argument.annotation),
        )
    return specs


def decode_arguments(
    resolver: Resolver, arguments: Mapping[str, Any], converter: NameConverter
) -> dict[str, Any]:
    """Convert an event's arguments into validated keyword arguments for a resolver.

    An omitted argument takes its parameter's default, or None if it has no default
    and accepts None. An explicit null takes the default only if the parameter
    doesn't accept None.

    Args:
        resolver: The resolver being called
        arguments: The event's arguments, by GraphQL name
        converter: The router's name converter

    Returns:
        The keyword arguments, by Python name

    Raises:
        ArgumentError: If an argument is unknown, missing, or fails validation
    """
    specs = _argument_specs(resolver, converter)
    unknown = sorted(arguments.keys() - specs.keys())
    if unknown:
        raise ArgumentError(f"Unknown argument(s): {', '.join(unknown)}")

    kwargs = {}
    for graphql_name, spec in specs.items():
        value = arguments.get(graphql_name)

        # Omitted and null arguments fall back to the default, or to None
        if value is None:
            if spec.argument.has_default and not (
                spec.nullable and graphql_name in arguments
            ):
                continue
            if not spec.nullable:
                raise ArgumentError(f"Argument {graphql_name!r} is required")
            kwargs[spec.python_name] = None
            continue

        # Decode GraphQL's representation, then validate by Python field names
        try:
            decoded = _decode(value, spec.argument.annotation, converter)
            kwargs[spec.python_name] = spec.adapter.validate_python(
                decoded, by_alias=False, by_name=True
            )
        except ArgumentError as error:
            raise ArgumentError(f"Argument {graphql_name!r}: {error}") from error
        except pydantic.ValidationError as error:
            raise ArgumentError(f"Argument {graphql_name!r}: {error}") from error
    return kwargs


def _selection_tree(selection_set_list: list[str]) -> Selection:
    """Build a selection tree from AppSync's `selectionSetList`.

    E.g. `["name", "posts", "posts/content"]` is
    `{"name": {}, "posts": {"content": {}}}`.
    """
    tree: Selection = {}
    for path in selection_set_list:
        node = tree
        for name in path.split("/"):
            node = node.setdefault(name, {})
    return tree


def _jsonable(value: Any) -> Any:
    """Make an `AWSJSON` value serialisable, keeping numbers as numbers.

    DynamoDB returns numbers as `Decimal`, which `json` can't serialise and pydantic
    would turn into strings.
    """
    if isinstance(value, Mapping):
        return {str(key): _jsonable(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(entry) for entry in value]
    if isinstance(value, decimal.Decimal):
        # Whole numbers stay integers, so `1` isn't returned as `1.0`
        return int(value) if value == value.to_integral_value() else float(value)
    return pydantic_core.to_jsonable_python(value)


def _encode(
    value: Any, annotation: Any, selection: Selection, converter: NameConverter
) -> Any:
    """Convert a value into JSON for AppSync, reading only the selected fields.

    Raises:
        TypeError: If a model field holds something other than that model
    """
    if value is None:
        return None
    annotation, _ = unwrap_annotation(annotation)
    origin = typing.get_origin(annotation)

    # Lists are encoded item by item, with the same selection
    if origin in (list, set, frozenset):
        (item,) = typing.get_args(annotation)
        return [_encode(entry, item, selection, converter) for entry in value]

    if annotation is dict or origin is dict:
        return _jsonable(value)

    # Scalars that JSON can't represent directly
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()

    if not isinstance(annotation, type):
        return value

    # GraphQL enum values are member names; accept a raw value from `use_enum_values`
    if issubclass(annotation, enum.Enum):
        member = value if isinstance(value, annotation) else annotation(value)
        return member.name

    # Objects are read field by field, so unselected properties never run
    if issubclass(annotation, pydantic.BaseModel):
        # A generic model may be returned unparametrised, e.g. `Page` for `Page[User]`
        origin_model = annotation.__pydantic_generic_metadata__["origin"] or annotation
        if not isinstance(value, origin_model):
            raise TypeError(
                f"Expected {annotation.__name__}, got {type(value).__name__}"
            )
        fields = _fields(annotation, converter, False)
        encoded = {}
        for name, subselection in selection.items():
            # Names AppSync resolves itself, such as __typename, aren't fields
            if name not in fields:
                continue
            field = fields[name]
            encoded[name] = _encode(
                getattr(value, field.python_name),
                field.annotation,
                subselection,
                converter,
            )
        return encoded

    return value


def encode_result(
    value: Any,
    annotation: Any,
    selection_set_list: list[str],
    converter: NameConverter,
) -> Any:
    """Convert what a resolver returned into the JSON AppSync expects.

    Args:
        value: The resolver's return value
        annotation: The resolver's return annotation
        selection_set_list: The event's `info.selectionSetList`
        converter: The router's name converter

    Raises:
        TypeError: If a model field holds something other than that model
    """
    selection = _selection_tree(selection_set_list)
    return _encode(value, annotation, selection, converter)
