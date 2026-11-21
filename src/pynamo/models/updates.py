"""Update actions, and the validated update expressions `DynamoModel.update` sends.

Values passed to `update` are validated against the model's field types and field
validators before anything is sent. A mapping given for a field that holds a model
updates only the keys it names (recursively), leaving the rest of the stored map as it
is; any other value, including a model instance, replaces the attribute whole. E.g.

    User.update({"user_id": "u1"}, {"identity": {"username": "new"}, "likes": Increment(1)})

`UpdateAction`s such as `Increment` are computed by DynamoDB from the stored value.
`_UpdateExpression` builds the update expression for both `update` and `put`.
"""

import dataclasses
import decimal
import functools
import types
import typing
from collections.abc import Callable, Mapping
from typing import Any

import pydantic


class UpdateAction:
    """An update DynamoDB computes from the attribute's stored value."""

    def expression(self, path: str, placeholder: Callable[[Any], str]) -> str:
        """Get the SET clause that updates the attribute.

        Args:
            path: The attribute's placeholder path, e.g. `#u0.#u1`
            placeholder: Adds a value to the expression and returns its placeholder
        """
        raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class Increment(UpdateAction):
    """Add `amount` to a numeric attribute, treating a missing attribute as 0.

    A negative amount decrements it.
    """

    amount: int | decimal.Decimal = 1

    def __post_init__(self):
        """Check the amount is a number DynamoDB accepts.

        Raises:
            TypeError: If `amount` isn't an int or Decimal
        """
        # bool is an int subclass, but almost certainly a mistake here
        amount = self.amount
        if isinstance(amount, (int, decimal.Decimal)) and not isinstance(amount, bool):
            return
        raise TypeError(f"Increment amount must be an int or Decimal, not {amount!r}")

    def expression(self, path: str, placeholder: Callable[[Any], str]) -> str:
        """Get `path = if_not_exists(path, 0) + amount`."""
        zero = placeholder(0)
        return f"{path} = if_not_exists({path}, {zero}) + {placeholder(self.amount)}"


class _UpdateExpression:
    """Collects the SET and REMOVE clauses of an update and their placeholders.

    Every name and value is a placeholder, as names may be reserved words. boto3 uses
    #n/:v for condition expressions, so these use #u/:u.
    """

    def __init__(self):
        """Start an expression with no clauses."""
        self._name_placeholders: dict[str, str] = {}
        self._values: dict[str, Any] = {}
        self._set_clauses: list[str] = []
        self._remove_clauses: list[str] = []

    def _name(self, name: str) -> str:
        """Get the placeholder for an attribute name, reusing it if seen before."""
        placeholder = self._name_placeholders.get(name)
        if placeholder is None:
            placeholder = f"#u{len(self._name_placeholders)}"
            self._name_placeholders[name] = placeholder
        return placeholder

    def _value(self, value: Any) -> str:
        """Get a new placeholder for a value."""
        placeholder = f":u{len(self._values)}"
        self._values[placeholder] = value
        return placeholder

    def set(self, path: tuple[str, ...], value: Any):
        """Add a clause setting the attribute at `path` to a value or `UpdateAction`.

        Args:
            path: The attribute names from the top-level attribute down
            value: The value as stored, or an action computing it
        """
        placeholder_path = ".".join(self._name(name) for name in path)
        if isinstance(value, UpdateAction):
            self._set_clauses.append(value.expression(placeholder_path, self._value))
        else:
            self._set_clauses.append(f"{placeholder_path} = {self._value(value)}")

    def add_changes(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
        """Add clauses turning the stored item `before` into `after`.

        Top-level attributes that are new or changed are set; those gone are removed.

        Returns:
            Whether any attribute differs, so there is anything to update
        """
        # The sentinel marks attributes absent from `before`
        changed = [
            name for name, value in after.items() if before.get(name, ...) != value
        ]
        removed = [name for name in before if name not in after]

        for name in changed:
            self.set((name,), after[name])
        for name in removed:
            self._remove_clauses.append(self._name(name))
        return bool(changed or removed)

    def arguments(self) -> dict[str, Any]:
        """Get the `update_item` arguments for the clauses added."""
        clauses = []
        if self._set_clauses:
            clauses.append("SET " + ", ".join(self._set_clauses))
        if self._remove_clauses:
            clauses.append("REMOVE " + ", ".join(self._remove_clauses))

        arguments: dict[str, Any] = {
            "UpdateExpression": " ".join(clauses),
            "ExpressionAttributeNames": {
                placeholder: name
                for name, placeholder in self._name_placeholders.items()
            },
        }

        # DynamoDB rejects an empty value map, which a REMOVE-only update would have
        if self._values:
            arguments["ExpressionAttributeValues"] = self._values
        return arguments


def _nested_model(annotation: Any) -> type[pydantic.BaseModel] | None:
    """Get the model a field holds (optionally None), or None if it isn't one model."""
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return annotation

    # Unwrap `Model | None`; unions of several types are only replaced whole
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        options = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(options) == 1:
            return _nested_model(options[0])
    return None


class _Fields:
    """A model's fields as `update` needs them, prepared once per model.

    Stored names are read when the model is defined. Nested models and the validator
    are prepared on first use, as field types may be forward references until then.

    Attributes:
        model: The model the fields belong to
        attributes: Each field's stored attribute name, by field name
    """

    def __init__(self, model: type[pydantic.BaseModel]):
        """Read the stored name of each of a model's fields."""
        self.model = model
        self.attributes = {
            name: field.serialization_alias or field.alias or name
            for name, field in model.model_fields.items()
        }

    @functools.cached_property
    def _nested(self) -> dict[str, _Fields]:
        """The prepared fields of each field that holds a model, by field name."""
        nested = {}
        for name, field in self.model.model_fields.items():
            model = _nested_model(field.annotation)
            if model is not None:
                nested[name] = _Fields(model)
        return nested

    @functools.cached_property
    def _validator(self) -> type[pydantic.BaseModel]:
        """A copy of the model with its fields and field validators, but no model validators.

        Assigning to the copy validates one value on its own, which model validators
        can't do, as they expect the rest of the model to be there.
        """
        # Re-declare each field validator, as create_model can't inherit them without
        # the model validators
        validators = {
            f"_validate_{decorator.cls_var_name}": pydantic.field_validator(
                *decorator.info.fields, mode=decorator.info.mode, check_fields=False
            )(decorator.func)
            for decorator in self.model.__pydantic_decorators__.field_validators.values()
        }
        fields: dict[str, Any] = {
            name: (field.annotation, field)
            for name, field in self.model.model_fields.items()
        }
        return pydantic.create_model(
            f"{self.model.__name__}Fields",
            __config__=self.model.model_config,
            __validators__=validators,
            **fields,
        )

    def _validate(self, name: str, value: Any) -> tuple[Any, Any]:
        """Validate a value for one of the fields.

        Returns:
            The validated value, and the value as it is stored

        Raises:
            pydantic.ValidationError: If the field's type or field validators reject it
        """
        # Assignment validation sets the value on the instance, as BaseModel.__setattr__
        # does
        instance = self._validator.model_construct()
        self._validator.__pydantic_validator__.validate_assignment(
            instance, name, value
        )
        # Dump just this field, as `put` would store it
        (stored,) = instance.model_dump(include={name}, by_alias=True).values()
        return getattr(instance, name), stored

    def collect(
        self,
        updates: Mapping[str, Any],
        path: tuple[str, ...],
        expression: _UpdateExpression,
    ) -> dict[str, Any]:
        """Validate updates to the fields and add a SET clause for each.

        Args:
            updates: Values, mappings of nested updates or `UpdateAction`s, by field name
            path: The stored attribute names leading to the model, empty at the top level
            expression: The expression to add the clauses to

        Returns:
            The validated value of each field set whole by a plain value, by field name

        Raises:
            TypeError: If a field doesn't exist on the model
            ValueError: If a nested update mapping is empty
            pydantic.ValidationError: If a value is invalid for its field
        """
        unknown = sorted(updates.keys() - self.attributes.keys())
        if unknown:
            raise TypeError(
                f"{self.model.__name__} has no field(s) to update: {', '.join(unknown)}"
            )

        validated = {}
        for name, value in updates.items():
            field_path = (*path, self.attributes[name])
            nested = self._nested.get(name)

            if isinstance(value, UpdateAction):
                # DynamoDB computes the value, so it's only validated when read back
                expression.set(field_path, value)
            elif isinstance(value, Mapping) and nested is not None:
                # A mapping for a model field updates only the keys it names
                if not value:
                    raise ValueError(f"The update to {'.'.join(field_path)} is empty")
                nested.collect(value, field_path, expression)
            else:
                # Anything else replaces the attribute whole
                validated[name], stored = self._validate(name, value)
                expression.set(field_path, stored)
        return validated
