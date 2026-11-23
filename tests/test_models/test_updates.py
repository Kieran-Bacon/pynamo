"""Tests for partial updates with `DynamoModel.update` and `pynamo.models.updates`."""

import botocore.exceptions
import pydantic
import pytest

from pynamo.models import DynamoModel, single_table_indexes
from pynamo.models.updates import Increment


class Identity(pydantic.BaseModel):
    """A nested model, for partial updates."""

    username: str | None
    email: str | None = pydantic.Field(alias="Email")

    @pydantic.field_validator("username")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        """Store usernames in lower case."""
        return value.lower() if value is not None else None


class Account(DynamoModel):
    """An account with a global index built from several attributes."""

    account_id: str
    plan: str
    region: str
    likes: int = pydantic.Field(default=0, ge=0)
    identity: Identity | None = None

    class Meta:
        indexes = single_table_indexes(
            base=("ACCOUNT#{account_id}", "ACCOUNT"),
            global_indexes=[("PLAN#{plan}", "REGION#{region}#{account_id}#{plan}")],
        )

    @pydantic.model_validator(mode="after")
    def _check_region(self) -> Account:
        """A model validator that would fail on a partially validated model."""
        if self.region not in {"eu", "us"}:
            raise ValueError("unknown region")
        return self


ACCOUNT_ITEM = Account(account_id="a1", plan="free", region="eu").model_dump(
    by_alias=True
)
"""The stored item DynamoDB returns after each update."""

ACCOUNT_KEY = {"account_id": "a1"}
"""The attribute values account a1's primary key is built from."""


@pytest.fixture
def table(table):
    """A mock table whose updates return `ACCOUNT_ITEM`."""
    table.update_item.return_value = {"Attributes": ACCOUNT_ITEM}
    return table


@pytest.fixture
def update(table):
    """Update account a1 and get the arguments sent, with placeholders inlined."""

    def update(changes):
        Account.update(ACCOUNT_KEY, changes, table=table)
        arguments = table.update_item.call_args.kwargs
        expression = arguments["UpdateExpression"]
        # Replace longest placeholders first so #u10 is not clobbered by #u1
        placeholders = arguments["ExpressionAttributeNames"] | {
            key: repr(value)
            for key, value in arguments["ExpressionAttributeValues"].items()
        }
        for placeholder in sorted(placeholders, key=len, reverse=True):
            expression = expression.replace(placeholder, placeholders[placeholder])
        return arguments, expression

    return update


def test_sets_validated_values_on_an_existing_item(update, render):
    arguments, expression = update({"likes": "3"})
    assert expression == "SET likes = 3"
    assert arguments["Key"] == {"PK": "ACCOUNT#a1", "SK": "ACCOUNT"}
    assert render(arguments["ConditionExpression"]) == "attribute_exists(PK)"
    assert arguments["ReturnValues"] == "ALL_NEW"


def test_returns_the_updated_model_attached_and_checkpointed(table):
    account = Account.update(ACCOUNT_KEY, {"likes": 1}, table=table)
    assert account.model_dump() == Account.model_validate(ACCOUNT_ITEM).model_dump()
    assert account._table is table
    assert account._checkpoint is not None


def test_invalid_values_and_unknown_fields_are_rejected(table):
    with pytest.raises(pydantic.ValidationError):
        Account.update(ACCOUNT_KEY, {"likes": -1}, table=table)
    with pytest.raises(TypeError, match="colour"):
        Account.update(ACCOUNT_KEY, {"colour": "red"}, table=table)
    with pytest.raises(TypeError, match="nickname"):
        Account.update(ACCOUNT_KEY, {"identity": {"nickname": "x"}}, table=table)
    table.update_item.assert_not_called()


def test_model_validators_are_not_run_on_the_partial_update(update):
    _, expression = update({"likes": 2})
    assert expression == "SET likes = 2"


def test_mappings_update_only_the_named_nested_keys(update):
    _, expression = update({"identity": {"username": "NEW", "email": None}})
    assert expression == "SET identity.username = 'new', identity.Email = None"


def test_model_instances_replace_the_attribute(update):
    _, expression = update({"identity": Identity(username="New", Email="a@b.c")})
    assert expression == "SET identity = {'username': 'new', 'Email': 'a@b.c'}"


def test_increment_starts_missing_attributes_at_zero(update):
    _, expression = update({"likes": Increment(2)})
    assert expression == "SET likes = if_not_exists(likes, 0) + 2"


@pytest.mark.parametrize("amount", [1.5, "1", True])
def test_increment_amounts_must_be_ints_or_decimals(amount):
    with pytest.raises(TypeError, match="int or Decimal"):
        Increment(amount)


def test_index_keys_built_from_updated_attributes_are_rewritten(update):
    _, expression = update({"plan": "pro", "region": "us"})
    assert expression == (
        "SET plan = 'pro', region = 'us', GSI1PK = 'PLAN#pro', "
        "GSI1SK = 'REGION#us#a1#pro'"
    )


def test_index_keys_need_every_attribute_they_are_built_from(table):
    with pytest.raises(ValueError, match="Not all required attributes"):
        Account.update(ACCOUNT_KEY, {"region": "us"}, table=table)
    table.update_item.assert_not_called()


def test_primary_key_attributes_cannot_be_changed(table):
    with pytest.raises(
        ValueError, match="can't update primary key attribute.*account_id"
    ):
        Account.update(ACCOUNT_KEY, {"account_id": "a2"}, table=table)
    table.update_item.assert_not_called()


def test_an_update_needs_changes(table):
    with pytest.raises(ValueError, match="at least one change"):
        Account.update(ACCOUNT_KEY, {}, table=table)
    with pytest.raises(ValueError, match="at least one change"):
        Account.update(ACCOUNT_KEY, table=table)
    table.update_item.assert_not_called()


def test_an_update_needs_no_key_when_the_key_uses_no_attributes(table):
    class Settings(DynamoModel):
        theme: str

        class Meta:
            indexes = single_table_indexes(base=("SETTINGS", "SETTINGS"))

    table.update_item.return_value = {"Attributes": {"theme": "dark"}}

    assert Settings.update(changes={"theme": "dark"}, table=table).theme == "dark"
    assert table.update_item.call_args.kwargs["Key"] == {
        "PK": "SETTINGS",
        "SK": "SETTINGS",
    }


def test_an_update_without_a_key_still_requires_the_key_attributes(table):
    with pytest.raises(ValueError, match="Not all required attributes"):
        Account.update(changes={"likes": 1}, table=table)
    table.update_item.assert_not_called()


def test_a_missing_item_is_a_key_error(table):
    table.update_item.side_effect = botocore.exceptions.ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
        "UpdateItem",
    )
    with pytest.raises(KeyError, match="Could not find entry"):
        Account.update(ACCOUNT_KEY, {"likes": 1}, table=table)
