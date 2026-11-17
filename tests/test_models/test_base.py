"""Tests for reading and writing `DynamoModel`s with `tidal.models.base`."""

from typing import ClassVar
from unittest.mock import Mock

import pytest

from tidal.models import (
    DynamoModel,
    GlobalSecondaryIndex,
    LocalSecondaryIndex,
    PrimaryIndex,
    gte,
    single_table_indexes,
)

ORDER_KEY = {"PK": "CUSTOMER#c1", "SK": "ORDER#2024-01-01#o1"}
"""The primary key of the item `order_item` returns."""


@pytest.fixture
def order(order_model, order_item):
    """An order that isn't attached to a table."""
    return order_model.model_validate(order_item())


@pytest.fixture
def read_order(order_model, order_item, table):
    """Read an order from the table, so it's attached and checkpointed."""
    table.get_item.return_value = {"Item": order_item()}
    return order_model.get(table, customer_id="c1", date="2024-01-01", order_id="o1")


def test_subclasses_must_include_a_primary_index():
    with pytest.raises(ValueError, match="must include a PrimaryIndex"):

        class ModelWithoutPrimaryIndex(DynamoModel):
            indexes: ClassVar[
                list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex]
            ] = [
                LocalSecondaryIndex(
                    index_name="ByName", sk_name="LSI1SK", sk_template="USER#{name}"
                )
            ]

            name: str


# Query


def test_query_rejects_unknown_attributes_and_indexes(order_model, table):
    with pytest.raises(TypeError, match="colour"):
        next(order_model.query({"customer_id": "c1", "colour": "red"}, table=table))
    with pytest.raises(ValueError, match="no index named 'GSI9'"):
        next(order_model.query({"customer_id": "c1"}, "GSI9", table=table))


def test_query_attributes_can_share_names_with_parameters(table, render):
    class Setting(DynamoModel):
        indexes = single_table_indexes(base=("TABLE#{table}", "LIMIT#{limit}"))

        table: str
        limit: int
        index_name: str

    next(
        Setting.query(
            {"table": "t1", "limit": gte(5), "index_name": "i1"}, table=table, limit=10
        )
    )
    request = table.query.call_args.kwargs

    assert request["Limit"] == 10
    assert "IndexName" not in request
    assert render(request["KeyConditionExpression"], is_key_condition=True) == (
        "(PK = 'TABLE#t1' AND SK BETWEEN 'LIMIT#5' AND 'LIMIT#\U0010ffff')"
    )
    assert render(request["FilterExpression"]) == "index_name = 'i1'"


def test_query_yields_a_page_per_request_until_there_is_no_token(
    order_model, order_item, table
):
    table.query.side_effect = [
        {"Items": [order_item()], "LastEvaluatedKey": {"PK": "a"}},
        {"Items": [order_item(), order_item()]},
    ]

    pages = list(order_model.query({"customer_id": "c1"}, table=table, limit=3))

    # Each request passes the limit straight to DynamoDB and resumes from the last
    first_request, second_request = (call.kwargs for call in table.query.call_args_list)
    assert first_request["Limit"] == 3
    assert "ExclusiveStartKey" not in first_request
    assert second_request["Limit"] == 3
    assert second_request["ExclusiveStartKey"] == {"PK": "a"}

    # Items are attached models, and the last page has no token
    assert [len(page.items) for page in pages] == [1, 2]
    assert pages[0].items[0]._table is table
    assert [page.token for page in pages] == [{"PK": "a"}, None]


def test_query_collects_invalid_items_on_the_page(order_model, order_item, table):
    invalid = order_item(total="lots")
    table.query.return_value = {"Items": [order_item(), invalid]}

    page = next(order_model.query({"customer_id": "c1"}, table=table))

    # The valid item is still returned alongside the failure
    assert len(page.items) == 1
    assert len(page.errors) == 1
    assert page.errors[0].item == invalid
    assert page.errors[0].error.errors()[0]["loc"] == ("total",)


# Put


def test_put_writes_the_attributes_and_every_index_key(order, order_item, table):
    order.put(table)

    assert table.put_item.call_args.kwargs["Item"] == order_item() | {
        **ORDER_KEY,
        "LSI1SK": "ORDER_BY_STATUS#open#2024-01-01",
        "GSI1PK": "ORDER#o1",
        "GSI1SK": "ORDER",
    }


def test_put_to_the_attached_table_checkpoints(order, table):
    order.attach(table).put()
    assert order._checkpoint == table.put_item.call_args.kwargs["Item"]


def test_put_to_another_table_is_a_one_off(order, table):
    # An unattached model stays unattached and unchecked
    order.put(table)
    assert order._table is None
    assert order._checkpoint is None

    # An attached model keeps its table and checkpoint
    order.attach(table).put()
    checkpoint = order._checkpoint
    other = Mock()
    order.status = "shipped"
    order.put(other)
    other.put_item.assert_called_once()
    assert order._table is table
    assert order._checkpoint == checkpoint

    # So the attached table is still updated with the change
    order.put()
    table.update_item.assert_called_once()


def test_put_defaults_to_the_attached_table(order_model, order_item, order, table):
    order.attach(table).put()
    table.put_item.assert_called_once()

    with pytest.raises(ValueError, match="has no table"):
        order_model.model_validate(order_item()).put()


def test_put_key_templates_can_use_properties(table):
    class Event(DynamoModel):
        indexes = single_table_indexes(base=("SOURCE#{source}", "EVENT#{day}"))

        source: str
        at: str

        @property
        def day(self) -> str:
            """The date part of `at`."""
            return self.at[:10]

    Event(source="s1", at="2024-01-01T09:00").put(table)
    item = table.put_item.call_args.kwargs["Item"]
    assert (item["PK"], item["SK"]) == ("SOURCE#s1", "EVENT#2024-01-01")


def test_get_reads_back_what_put_wrote(order_model, order, table):
    order.attach(table).put()
    table.get_item.return_value = {"Item": table.put_item.call_args.kwargs["Item"]}

    read = order_model.get(table, customer_id="c1", date="2024-01-01", order_id="o1")
    assert read == order


def test_put_on_a_checkpointed_model_only_updates_what_changed(order, table, render):
    order.attach(table).put()
    order.status = "shipped"
    order.total = 2
    order.put()

    # Only the changed attributes, and the index key built from status, are set
    table.put_item.assert_called_once()
    arguments = table.update_item.call_args.kwargs
    assert arguments["Key"] == ORDER_KEY
    assert arguments["UpdateExpression"] == "SET #u0 = :u0, #u1 = :u1, #u2 = :u2"
    assert arguments["ExpressionAttributeNames"] == {
        "#u0": "status",
        "#u1": "total",
        "#u2": "LSI1SK",
    }
    assert arguments["ExpressionAttributeValues"] == {
        ":u0": "shipped",
        ":u1": 2,
        ":u2": "ORDER_BY_STATUS#shipped#2024-01-01",
    }
    assert render(arguments["ConditionExpression"]) == "attribute_exists(PK)"

    # The new state is checkpointed, so putting again sends nothing
    order.put()
    table.update_item.assert_called_once()


def test_put_on_a_detached_model_writes_it_whole(order, table):
    order.attach(table).put()
    order.attach(None)
    order.put(table)
    assert table.put_item.call_count == 2
    table.update_item.assert_not_called()


def test_put_rejects_a_changed_primary_key(order, table):
    order.attach(table).put()
    order.order_id = "o2"
    with pytest.raises(ValueError, match="primary key has changed"):
        order.put()


# Delete


def test_delete_uses_the_primary_key(order, table):
    order.delete(table)
    table.delete_item.assert_called_once_with(Key=ORDER_KEY)


def test_delete_uses_the_checkpointed_key_when_the_key_has_changed(read_order, table):
    read_order.order_id = "o2"
    read_order.delete()
    table.delete_item.assert_called_once_with(Key=ORDER_KEY)


def test_delete_drops_the_checkpoint_but_stays_attached(read_order, table):
    read_order.delete()
    assert read_order._checkpoint is None
    assert read_order._table is table


def test_delete_from_another_table_keeps_the_checkpoint(read_order):
    checkpoint = read_order._checkpoint
    other = Mock()
    read_order.delete(other)
    other.delete_item.assert_called_once()
    assert read_order._checkpoint == checkpoint


def test_delete_requires_a_table(order):
    with pytest.raises(ValueError, match="has no table"):
        order.delete()


# Remove


def test_remove_deletes_by_the_key_built_from_the_values(order_model, table):
    order_model.remove(table, customer_id="c1", date="2024-01-01", order_id="o1")
    table.delete_item.assert_called_once_with(Key=ORDER_KEY)


def test_get_and_remove_accept_a_key_attribute_named_table(table):
    class Setting(DynamoModel):
        indexes = single_table_indexes(base=("TABLE#{table}", "SETTING"))

        table: str

    table.get_item.return_value = {"Item": {"table": "t1"}}
    key = {"PK": "TABLE#t1", "SK": "SETTING"}

    assert Setting.get(table, table="t1").table == "t1"
    table.get_item.assert_called_once_with(Key=key)
    Setting.remove(table, table="t1")
    table.delete_item.assert_called_once_with(Key=key)


def test_remove_requires_the_key_attributes(order_model, table):
    with pytest.raises(KeyError):
        order_model.remove(table, customer_id="c1", date="2024-01-01")
    table.delete_item.assert_not_called()


# Scan


def test_scan_passes_request_options_to_dynamodb(order_model, table):
    next(
        order_model.scan(
            {"status": "open"},
            "GSI1",
            table=table,
            limit=5,
            consistent_read=True,
            start_token={"PK": "a"},
        )
    )
    request = table.scan.call_args.kwargs
    assert request["IndexName"] == "GSI1"
    assert request["Limit"] == 5
    assert request["ConsistentRead"] is True
    assert request["ExclusiveStartKey"] == {"PK": "a"}


def test_scan_rejects_unknown_attributes_indexes_and_limits(order_model, table):
    with pytest.raises(TypeError, match="colour"):
        next(order_model.scan({"colour": "red"}, table=table))
    with pytest.raises(ValueError, match="no index named 'GSI9'"):
        next(order_model.scan(index_name="GSI9", table=table))
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        next(order_model.scan(limit=0, table=table))


def test_scan_yields_pages_and_collects_invalid_items(order_model, order_item, table):
    invalid = order_item(total="lots")
    table.scan.side_effect = [
        {"Items": [order_item()], "LastEvaluatedKey": {"PK": "a"}},
        {"Items": [order_item(), invalid]},
    ]

    pages = list(order_model.scan(table=table))

    # The second request resumes from the first page's token
    second_request = table.scan.call_args_list[1].kwargs
    assert second_request["ExclusiveStartKey"] == {"PK": "a"}

    # Valid items are attached models; invalid ones are on the page's errors
    assert [len(page.items) for page in pages] == [1, 1]
    assert pages[0].items[0]._table is table
    assert [page.errors[0].item for page in pages[1:]] == [invalid]
    assert [page.token for page in pages] == [{"PK": "a"}, None]


# Default tables


@pytest.fixture
def models(table):
    """A base model and its subclass, which inherit default tables."""

    class Base(DynamoModel):
        indexes = single_table_indexes(base=("ITEM#{item_id}", "ITEM"))

        item_id: str

    class Child(Base):
        pass

    table.get_item.return_value = {"Item": {"item_id": "i1"}}
    return Base, Child


def test_subclasses_inherit_a_parents_default_table(models, table):
    base, child = models
    base.default_table(table)
    assert child.default_table() is table

    # Items read from the default table are attached to it
    item = child.get(item_id="i1")
    table.get_item.assert_called_once()
    assert item._table is table


def test_a_subclass_default_table_does_not_change_its_parent(models, table):
    base, child = models
    base.default_table(table)
    other = Mock()
    child.default_table(other)
    assert base.default_table() is table
    assert child.default_table() is other


def test_a_missing_default_table_is_reported(models):
    _, child = models
    with pytest.raises(ValueError, match="Child has no table"):
        child.get(item_id="i1")


def test_attached_and_given_tables_take_precedence_over_the_default(models, table):
    base, child = models
    base.default_table(table)
    attached, given = Mock(), Mock()

    # An unattached model uses the default without attaching to it
    item = child(item_id="i1")
    item.put()
    table.put_item.assert_called_once()
    assert item._table is None

    item.attach(attached).put()
    attached.put_item.assert_called_once()
    item.put(given)
    given.put_item.assert_called_once()
