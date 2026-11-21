"""Tests for index keys without a template, which are the model field of the same name."""

from typing import ClassVar

import pydantic
import pytest

from tidal.models import DynamoModel, GlobalSecondaryIndex, KeyPair, PrimaryIndex
from tidal.models.conditions import GreaterThan, NotEqual


class Score(DynamoModel):
    """A score keyed by its own columns, with a global index on a templated key."""

    player_id: str
    points: int
    game: str

    class Meta:
        indexes: ClassVar[list] = [
            PrimaryIndex(KeyPair("player_id"), KeyPair("points")),
            GlobalSecondaryIndex(
                "ByGame", KeyPair("GSI1PK", "GAME#{game}"), KeyPair("points")
            ),
        ]


def test_put_stores_column_keys_once_as_the_fields_values(table):
    Score(player_id="p1", points=5, game="chess").put(table)

    # Only the templated key is added; column keys keep their type
    assert table.put_item.call_args.kwargs["Item"] == {
        "player_id": "p1",
        "points": 5,
        "game": "chess",
        "GSI1PK": "GAME#chess",
    }


def test_get_passes_column_key_values_through(table):
    table.get_item.return_value = {
        "Item": {"player_id": "p1", "points": 5, "game": "chess"}
    }

    Score.get({"player_id": "p1", "points": 5}, table=table)

    assert table.get_item.call_args.kwargs["Key"] == {"player_id": "p1", "points": 5}


def test_query_applies_comparators_to_a_column_sort_key(table, render):
    next(Score.query({"player_id": "p1", "points": GreaterThan(5)}, table=table))
    request = table.query.call_args.kwargs

    assert render(request["KeyConditionExpression"], is_key_condition=True) == (
        "(player_id = 'p1' AND points > 5)"
    )
    assert "FilterExpression" not in request


def test_filter_only_comparators_on_a_column_sort_key_are_filtered(table, render):
    next(Score.query({"player_id": "p1", "points": NotEqual(5)}, table=table))
    request = table.query.call_args.kwargs

    assert render(request["KeyConditionExpression"], is_key_condition=True) == (
        "player_id = 'p1'"
    )
    assert render(request["FilterExpression"]) == "points <> 5"


def test_query_mixes_template_and_column_keys(table, render):
    next(Score.query({"game": "chess", "points": 5}, "ByGame", table=table))
    request = table.query.call_args.kwargs

    assert render(request["KeyConditionExpression"], is_key_condition=True) == (
        "(GSI1PK = 'GAME#chess' AND points = 5)"
    )


def test_scan_has_no_prefix_filter_for_column_keys(table):
    next(Score.scan(table=table))

    assert "FilterExpression" not in table.scan.call_args.kwargs


def test_updating_a_column_key_sets_only_the_field(table):
    table.update_item.return_value = {
        "Attributes": {"player_id": "p1", "points": 5, "game": "go"}
    }

    Score.update({"player_id": "p1", "points": 5}, {"game": "go"}, table=table)
    arguments = table.update_item.call_args.kwargs

    # The templated key built from `game` is regenerated alongside it
    assert set(arguments["ExpressionAttributeNames"].values()) == {"game", "GSI1PK"}
    assert "GAME#go" in arguments["ExpressionAttributeValues"].values()


def test_column_keys_must_be_model_fields():
    with pytest.raises(ValueError, match="no field or computed field named 'points'"):

        class Missing(DynamoModel):
            player_id: str

            class Meta:
                indexes: ClassVar[list] = [
                    PrimaryIndex(KeyPair("player_id"), KeyPair("points"))
                ]


def test_column_keys_must_be_stored_under_their_own_name():
    with pytest.raises(ValueError, match="stored under its own name"):

        class Aliased(DynamoModel):
            player_id: str
            points: int = pydantic.Field(alias="Points")

            class Meta:
                indexes: ClassVar[list] = [
                    PrimaryIndex(KeyPair("player_id"), KeyPair("points"))
                ]
