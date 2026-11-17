"""Tests for reading a resolver's arguments and return type in `pynamo.graphql_api.resolvers`."""

from typing import Annotated

import pytest

from pynamo.graphql_api import AppSyncEvent, SchemaError
from pynamo.graphql_api.resolvers import Argument, Resolver


def test_parameters_become_arguments():
    def search(text: str, *, limit: int = 10) -> list[str]:
        raise NotImplementedError

    resolver = Resolver(search, "Query", "search")
    assert resolver.arguments == {
        "text": Argument(str, False),
        "limit": Argument(int, True),
    }


def test_annotated_metadata_is_kept():
    def search(text: Annotated[str, "metadata"]) -> str:
        raise NotImplementedError

    resolver = Resolver(search, "Query", "search")
    assert resolver.arguments["text"].annotation == Annotated[str, "metadata"]


def test_annotations_can_reference_later_classes():
    def latest() -> Later:
        raise NotImplementedError

    class Later:
        pass

    assert Resolver(latest, "Query", "latest").return_annotation is Later


def test_arguments_need_annotations():
    def untyped(user_id) -> str:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="'user_id' has no type annotation"):
        _ = Resolver(untyped, "Query", "untyped").arguments


def test_arguments_must_be_passable_by_keyword():
    def variadic(*names: str) -> str:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="must be passable by keyword"):
        _ = Resolver(variadic, "Query", "variadic").arguments


def test_undefined_names_are_reported():
    # The undefined name is deliberate, so the linters are told to ignore it
    def broken() -> Missing:  # noqa: F821  # ty: ignore[unresolved-reference]
        raise NotImplementedError

    with pytest.raises(SchemaError, match="can't be resolved"):
        _ = Resolver(broken, "Query", "broken").return_annotation


def test_event_parameter_is_not_an_argument():
    def me(event: AppSyncEvent, verbose: bool = False) -> str:
        raise NotImplementedError

    resolver = Resolver(me, "Query", "me")
    assert resolver.event_parameter == "event"
    assert list(resolver.arguments) == ["verbose"]


def test_no_event_parameter():
    def ping() -> str:
        raise NotImplementedError

    assert Resolver(ping, "Query", "ping").event_parameter is None


def test_only_one_event_parameter():
    def twice(first: AppSyncEvent, second: AppSyncEvent) -> str:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="more than one AppSyncEvent"):
        _ = Resolver(twice, "Query", "twice").event_parameter


def test_return_annotation_is_required():
    def missing(user_id: str):
        raise NotImplementedError

    with pytest.raises(SchemaError, match="must annotate the type it returns"):
        _ = Resolver(missing, "Query", "missing").return_annotation


def test_none_is_not_a_return_type():
    def nothing() -> None:
        raise NotImplementedError

    with pytest.raises(SchemaError, match="must annotate the type it returns"):
        _ = Resolver(nothing, "Query", "nothing").return_annotation
