"""Fixtures shared by the `tidal.models` tests."""

from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

import pytest
from boto3.dynamodb.conditions import ConditionBase, ConditionExpressionBuilder

from tidal.models import DynamoModel, single_table_indexes


@pytest.fixture
def table() -> Mock:
    """A mock DynamoDB table whose queries and scans find no items."""
    table = Mock()
    table.query.return_value = {"Items": []}
    table.scan.return_value = {"Items": []}
    return table


@pytest.fixture
def order_model() -> type[DynamoModel]:
    """An order model, defined afresh for each test so class state can't leak."""

    class Order(DynamoModel):
        """An order with a local index on its status and a global index on its ID."""

        customer_id: str
        order_id: str
        date: str
        status: str
        total: int

        class Meta:
            indexes = single_table_indexes(
                base=("CUSTOMER#{customer_id}", "ORDER#{date}#{order_id}"),
                local=["ORDER_BY_STATUS#{status}#{date}"],
                global_indexes=[("ORDER#{order_id}", "ORDER")],
            )

    return Order


@pytest.fixture
def order_item(order_model: type[DynamoModel]) -> Callable[..., dict[str, Any]]:
    """Get a stored order item, with any attributes overridden."""

    def order_item(**overrides: Any) -> dict[str, Any]:
        order = order_model.model_validate(
            {
                "customer_id": "c1",
                "order_id": "o1",
                "date": "2024-01-01",
                "status": "open",
                "total": 1,
            }
        )
        return order.model_dump() | overrides

    return order_item


@pytest.fixture
def render() -> Callable[..., str]:
    """Render a boto3 condition as its expression, with names and values inlined."""

    def render(condition: ConditionBase, is_key_condition: bool = False) -> str:
        built = ConditionExpressionBuilder().build_expression(
            condition, is_key_condition=is_key_condition
        )
        expression = built.condition_expression
        for placeholder, name in built.attribute_name_placeholders.items():
            expression = expression.replace(placeholder, name)
        # Replace longest placeholders first so :v10 is not clobbered by :v1
        for placeholder in sorted(
            built.attribute_value_placeholders, key=len, reverse=True
        ):
            value = built.attribute_value_placeholders[placeholder]
            rendered = f"'{value}'" if isinstance(value, str) else str(value)
            expression = expression.replace(placeholder, rendered)
        return expression

    return render
