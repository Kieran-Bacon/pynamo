"""Tests for defining key layouts with `pynamo.models.indexes`."""

from typing import ClassVar

import pytest

from pynamo.models import (
    DynamoModel,
    GlobalSecondaryIndex,
    KeyPair,
    LocalIndex,
    PrimaryIndex,
    single_table_indexes,
)


def test_key_attributes_are_in_template_order():
    assert KeyPair("SK", "ORDER#{date}#{order_id}#END").attributes == (
        "date",
        "order_id",
    )
    # A column key is built from its own field
    assert KeyPair("points").attributes == ("points",)


def test_extract_fields_takes_the_leading_key_values_in_order():
    key = KeyPair("SK", "ORDER#{date}#{order_id}")

    # Values stop at the first missing attribute, unless they are required
    assert key.extract_fields({"order_id": "o1", "x": 1}, required=False) == (
        {},
        {"order_id": "o1", "x": 1},
    )
    assert key.extract_fields({"date": "d", "order_id": "o1"}) == (
        {"date": "d", "order_id": "o1"},
        {},
    )
    with pytest.raises(ValueError, match="missing attribute order_id"):
        key.extract_fields({"date": "d"})


def test_index_format_never_builds_a_partial_partition_key():
    index = PrimaryIndex(KeyPair("PK", "USER#{user_id}"), KeyPair("SK", "POST#{id}"))

    # Only the sort key may be partial, so a missing partition value always raises
    assert index.format({"user_id": "u1"}, partial=True)["PK"] == "USER#u1"
    with pytest.raises(ValueError, match="Not all required attributes"):
        index.format({"id": "p1"}, partial=True)


def test_index_format_rejects_attributes_the_key_is_not_built_from():
    index = PrimaryIndex(KeyPair("PK", "USER#{user_id}"), KeyPair("SK", "USER"))

    with pytest.raises(ValueError, match="not built from the attribute.*name"):
        index.format({"user_id": "u1", "name": "n"})


def test_factory_sets_primary_and_secondary_indexes():
    class AuditLog(DynamoModel):
        audit_log_id: str
        user_id: str
        action_type: str
        timestamp: int

        class Meta:
            indexes = single_table_indexes(
                base=("AUDIT_LOG#{audit_log_id}", "AUDIT_LOG#{timestamp}"),
                local=["AUDIT_LOG_BY_ACTION#{action_type}"],
                global_indexes=[
                    ("AUDIT_LOG_BY_USER#{user_id}", "AUDIT_LOG#{timestamp}"),
                    ("AUDIT_LOG_BY_ACTION#{action_type}", "AUDIT_LOG#{timestamp}"),
                ],
            )

    primary = AuditLog._meta.primary_index
    assert (primary.partition.name, primary.partition.template) == (
        "PK",
        "AUDIT_LOG#{audit_log_id}",
    )
    assert (primary.sort.name, primary.sort.template) == ("SK", "AUDIT_LOG#{timestamp}")
    assert list(AuditLog._meta.secondary_indexes) == ["LSI1", "GSI1", "GSI2"]
    assert isinstance(AuditLog._meta.secondary_indexes["LSI1"], LocalIndex)
    assert isinstance(AuditLog._meta.secondary_indexes["GSI1"], GlobalSecondaryIndex)


def test_local_index_defines_only_its_sort_key():
    indexes = single_table_indexes(
        base=("USER#{user_id}", "USER#{created_at}"),
        local=["USER_BY_NAME#{name}"],
    )

    local = indexes[1]
    assert isinstance(local, LocalIndex)
    assert (local.name, local.sort.name) == ("LSI1", "LSI1SK")
    assert local.sort.template == "USER_BY_NAME#{name}"
    assert not hasattr(local, "partition")


def test_supports_custom_index_names_and_key_attributes(table):
    class EmailUser(DynamoModel):
        user_id: str
        email: str

        class Meta:
            indexes: ClassVar[list] = [
                PrimaryIndex(
                    KeyPair("PK", "USER#{user_id}"), KeyPair("SK", "USER#{user_id}")
                ),
                GlobalSecondaryIndex(
                    "ByEmail",
                    KeyPair("EMAIL_PK", "EMAIL#{email}"),
                    KeyPair("EMAIL_SK", "USER#{user_id}"),
                ),
            ]

    EmailUser(user_id="u1", email="a@example.com").put(table)
    item = table.put_item.call_args.kwargs["Item"]
    assert (item["EMAIL_PK"], item["EMAIL_SK"]) == ("EMAIL#a@example.com", "USER#u1")


def test_secondary_index_names_must_be_unique():
    with pytest.raises(ValueError, match="repeats the name 'ByName'"):

        class User(DynamoModel):
            name: str

            class Meta:
                indexes: ClassVar[list] = [
                    PrimaryIndex(KeyPair("PK", "USER#{name}"), KeyPair("SK", "USER")),
                    LocalIndex("ByName", KeyPair("LSI1SK", "NAME#{name}")),
                    LocalIndex("ByName", KeyPair("LSI2SK", "NAME#{name}")),
                ]


# Key template fields must name top-level attributes, so anything else is rejected
# when the model is defined
@pytest.mark.parametrize(
    "template", ["OWNER#{owner.id}", "TAG#{tags[0]}", "ITEM#{}", "ITEM#{0}"]
)
def test_template_fields_must_be_model_attributes(template: str):
    with pytest.raises(ValueError, match="missing required attributes"):

        class Item(DynamoModel):
            owner: dict
            tags: list

            class Meta:
                indexes = single_table_indexes(base=(template, "ITEM"))


def test_format_specs_and_conversions_are_allowed(table):
    class Event(DynamoModel):
        id: str
        timestamp: int

        class Meta:
            indexes = single_table_indexes(
                base=("EVENT#{id}", "EVENT"), local=["AT#{timestamp:010d}{id!s}"]
            )

    Event(id="e1", timestamp=42).put(table)
    assert table.put_item.call_args.kwargs["Item"]["LSI1SK"] == "AT#0000000042e1"
