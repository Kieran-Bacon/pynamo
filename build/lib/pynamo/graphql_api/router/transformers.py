import abc
from collections.abc import Callable
from typing import Any, Dict, Union


def recursively_apply(func, data: dict | list):
    if isinstance(data, dict):
        return {
            func(key): recursively_apply(func, value) for key, value in data.items()
        }
    elif isinstance(data, list):
        return [recursively_apply(func, value) for value in data]
    else:
        return data


class BaseTransformer(abc.ABC):
    @abc.abstractmethod
    def fieldTransformer(self, key: str) -> str:
        pass

    @abc.abstractmethod
    def inverseFieldTransformer(self, key: str) -> str:
        pass

    @abc.abstractmethod
    def argumentTransformer(self, key: str) -> str:
        pass

    @abc.abstractmethod
    def funcTransformer(self, key: str) -> str:
        pass

    @abc.abstractmethod
    def typeTransformer(self, key: str) -> str:
        pass

    @classmethod
    def applyTransformerRecursively(
        cls, transformer: Callable[[str], str], mapping: dict[str, Any]
    ) -> dict[str, Any]:

        new_mapping = {}
        for key, value in mapping.items():
            if isinstance(value, dict):
                value = cls.applyTransformerRecursively(transformer, value)

            new_mapping[transformer(key)] = value

        return new_mapping


from pydantic.alias_generators import to_camel, to_snake


class IdentityTransformer(BaseTransformer):
    def fieldTransformer(self, key: str) -> str:
        return key

    def argumentTransformer(self, key: str) -> str:
        return key

    def funcTransformer(self, key: str) -> str:
        return key

    def typeTransformer(self, key: str) -> str:
        return key


class SnakeToCamelCaseTransformer(BaseTransformer):
    def __init__(
        self,
        transform_fields: bool = True,
        transform_args: bool = True,
        transform_funcs: bool = True,
        transform_types: bool = True,
    ):

        self.transform_fields = transform_fields
        self.transform_args = transform_args
        self.transform_funcs = transform_funcs
        self.transform_types = transform_types

    def fieldTransformer(self, key: str) -> str:
        if not self.transform_fields:
            return key

        return to_camel(key)

    def inverseFieldTransformer(self, key: str) -> str:
        """Perform the inverse transformation of this classes field transformer"""
        if not self.transform_fields:
            return key
        return to_snake(key)

    def argumentTransformer(self, key):
        if not self.transform_args:
            return key

        return to_camel(key)

    def funcTransformer(self, key):
        if not self.transform_funcs:
            return key

        return to_camel(key)

    def typeTransformer(self, key):
        if not self.transform_types:
            return key
        return to_camel(key)
