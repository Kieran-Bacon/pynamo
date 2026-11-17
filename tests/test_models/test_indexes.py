"""Tests for defining key layouts with `tidal.models.indexes`."""

from typing import ClassVar

import pydantic
import pytest

from tidal.models import (
    DynamoModel,
    GlobalSecondaryIndex,
    LocalSecondaryIndex,
    PrimaryIndex,
    single_table_indexes,
)


def test_index_models_validate_non_empty_templates():
    with pytest.raises(pydantic.ValidationError):
        LocalSecondaryIndex(index_name="ByName", sk_name="LSI1SK", sk_template="")


def test_factory_sets_primary_and_secondary_indexes():
    class AuditLog(DynamoModel):
        indexes = single_table_indexes(
            base=("AUDIT_LOG#{audit_log_id}", "AUDIT_LOG#{timestamp}"),
            local=["AUDIT_LOG_BY_ACTION#{action_type}"],
            global_indexes=[
                ("AUDIT_LOG_BY_USER#{user_id}", "AUDIT_LOG#{timestamp}"),
                ("AUDIT_LOG_BY_ACTION#{action_type}", "AUDIT_LOG#{timestamp}"),
            ],
        )

        audit_log_id: str
        user_id: str
        action_type: str
        timestamp: int

    assert AuditLog._primary_index == PrimaryIndex(
        pk_name="PK",
        sk_name="SK",
        pk_template="AUDIT_LOG#{audit_log_id}",
        sk_template="AUDIT_LOG#{timestamp}",
    )
    assert isinstance(AuditLog.indexes, list)
    assert isinstance(AuditLog.indexes[0], PrimaryIndex)
    assert isinstance(AuditLog.indexes[1], LocalSecondaryIndex)
    assert not hasattr(AuditLog.indexes[1], "pk_name")


def test_local_index_defines_only_its_sort_key_template():
    indexes = single_table_indexes(
        base=("USER#{user_id}", "USER#{created_at}"),
        local=["USER_BY_NAME#{name}"],
    )

    assert isinstance(indexes[1], LocalSecondaryIndex)
    assert indexes[1].sk_name == "LSI1SK"
    assert indexes[1].sk_template == "USER_BY_NAME#{name}"
    assert not hasattr(indexes[1], "pk_name")


def test_supports_custom_index_names_and_key_attributes():
    custom_indexes: list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex] = [
        PrimaryIndex(
            pk_name="PK",
            sk_name="SK",
            pk_template="USER#{user_id}",
            sk_template="USER#{user_id}",
        ),
        GlobalSecondaryIndex(
            index_name="ByEmail",
            pk_name="EMAIL_PK",
            sk_name="EMAIL_SK",
            pk_template="EMAIL#{email}",
            sk_template="USER#{user_id}",
        ),
    ]

    class EmailUser(DynamoModel):
        indexes: ClassVar[
            list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex]
        ] = custom_indexes

        user_id: str
        email: str

    assert EmailUser._primary_index is custom_indexes[0]


# Key template fields must name top-level attributes. Nested fields such as
# `{owner.id}` are deliberately unsupported, so they're rejected when the index is defined
@pytest.mark.parametrize(
    "template", ["OWNER#{owner.id}", "TAG#{tags[0]}", "ITEM#{}", "ITEM#{0}"]
)
def test_nested_template_fields_are_rejected(template: str):
    with pytest.raises(pydantic.ValidationError, match="top-level"):
        single_table_indexes(base=(template, "ITEM"))


def test_format_specs_and_conversions_are_allowed():
    index = LocalSecondaryIndex(
        index_name="LSI1", sk_name="LSI1SK", sk_template="AT#{timestamp:010d}{id!s}"
    )
    assert index.sk_template == "AT#{timestamp:010d}{id!s}"
