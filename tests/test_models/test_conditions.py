"""Tests for building key conditions and filters from values in `pynamo.models.conditions`."""

from unittest.mock import Mock

import pytest

from pynamo.models import DynamoModel, single_table_indexes
from pynamo.models.conditions import (
    BeginsWith,
    Between,
    GreaterThan,
    GreaterThanOrEqual,
    LessThan,
    NotEqual,
)


@pytest.fixture
def query(order_model, table, render):
    """Query orders, returning the request with its key and filter rendered."""

    def query(*args, **kwargs):
        next(order_model.query(*args, table=table, **kwargs))
        request = table.query.call_args.kwargs
        key = render(request["KeyConditionExpression"], is_key_condition=True)
        filter_expression = request.get("FilterExpression")
        return request, key, filter_expression and render(filter_expression)

    return query


@pytest.fixture
def scan_filter(table, render):
    """Scan a model's items, returning the request's filter rendered."""

    def scan_filter(model, *args, **kwargs):
        next(model.scan(*args, table=table, **kwargs))
        return render(table.scan.call_args.kwargs["FilterExpression"])

    return scan_filter


def test_between_bounds_are_formatted_for_the_selected_index(table):
    class AuditLog(DynamoModel):
        audit_log_id: str
        user_id: str
        timestamp: str

        class Meta:
            indexes = single_table_indexes(
                base=("AUDIT_LOG#{audit_log_id}", "AUDIT_LOG#{timestamp}"),
                global_indexes=[
                    ("AUDIT_LOG_BY_USER#{user_id}", "AUDIT_LOG#{timestamp}")
                ],
            )

    page = next(
        AuditLog.query(
            {"user_id": "user-1", "timestamp": Between("2020", "2022")},
            index_name="GSI1",
            table=table,
        )
    )

    assert page.items == []
    query_args = table.query.call_args.kwargs
    assert query_args["IndexName"] == "GSI1"
    key_expression = query_args["KeyConditionExpression"].get_expression()
    partition_condition, sort_condition = key_expression["values"]
    partition_values = partition_condition.get_expression()["values"]
    assert partition_values[0].name == "GSI1PK"
    assert partition_values[1] == "AUDIT_LOG_BY_USER#user-1"
    sort_expression = sort_condition.get_expression()
    assert sort_expression["operator"] == "BETWEEN"
    assert sort_expression["values"][0].name == "GSI1SK"
    assert sort_expression["values"][1:] == ("AUDIT_LOG#2020", "AUDIT_LOG#2022")


def test_full_sort_key_is_an_equality(query):
    _, key, filters = query({"customer_id": "c1", "date": "2024", "order_id": "o1"})
    assert key == "(PK = 'CUSTOMER#c1' AND SK = 'ORDER#2024#o1')"
    assert filters is None


def test_missing_sort_key_attributes_match_on_the_fixed_prefix(query):
    _, key, filters = query({"customer_id": "c1"})
    assert key == "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))"
    assert filters is None

    _, key, _ = query({"customer_id": "c1", "date": "2024"})
    assert key == "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#2024#'))"


def test_range_on_a_middle_attribute_includes_keys_continuing_past_it(query):
    _, key, filters = query(
        {"customer_id": "c1", "date": Between("2024-01", "2024-03")}
    )
    # The upper bound reaches every key for 2024-03, e.g. ORDER#2024-03#o1
    assert key == (
        "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#2024-01#' "
        "AND 'ORDER#2024-03#\U0010ffff')"
    )
    assert filters is None


def test_second_sort_key_condition_goes_to_the_filter(query):
    _, key, filters = query(
        {
            "customer_id": "c1",
            "date": GreaterThanOrEqual("2024"),
            "order_id": BeginsWith("o"),
        }
    )
    assert key == (
        "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#2024#' AND 'ORDER#\U0010ffff')"
    )
    assert filters == "begins_with(order_id, 'o')"


def test_attribute_after_a_missing_one_goes_to_the_filter(query):
    _, key, filters = query({"customer_id": "c1", "order_id": "o1"})
    assert key == "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))"
    assert filters == "order_id = 'o1'"


def test_non_key_attributes_are_filtered(query):
    _, _, filters = query(
        {"customer_id": "c1", "status": "open", "total": GreaterThan(10)}
    )
    assert filters == "(status = 'open' AND total > 10)"


def test_one_sided_range_is_capped_by_the_key_text_before_it(query):
    _, key, filters = query({"customer_id": "c1", "date": LessThan("2024")})
    # The range starts at ORDER# and ends just below every key for 2024
    assert key == (
        "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#' AND 'ORDER#2024\"\U0010ffff')"
    )
    assert filters is None


def test_local_index_uses_the_table_partition_key(query):
    request, key, filters = query(
        {"customer_id": "c1", "status": "open", "date": Between("2024", "2025")},
        "LSI1",
    )
    assert request["IndexName"] == "LSI1"
    assert key == (
        "(PK = 'CUSTOMER#c1' AND LSI1SK BETWEEN "
        "'ORDER_BY_STATUS#open#2024' AND 'ORDER_BY_STATUS#open#2025')"
    )
    assert filters is None


def test_filter_only_comparator_on_the_sort_key(query):
    _, key, filters = query({"customer_id": "c1", "date": NotEqual("2024")})
    assert key == "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))"
    assert filters == "date <> '2024'"


def test_partition_key_values_are_required(order_model, table: Mock):
    with pytest.raises(ValueError, match="missing attribute order_id"):
        next(order_model.query({"customer_id": "c1"}, "GSI1", table=table))
    with pytest.raises(ValueError, match="must be plain values"):
        next(order_model.query({"customer_id": BeginsWith("c")}, table=table))


def test_scan_without_values_matches_primary_key_prefixes(order_model, scan_filter):
    assert scan_filter(order_model) == (
        "(begins_with(PK, 'CUSTOMER#') AND begins_with(SK, 'ORDER#'))"
    )


def test_scan_keys_without_a_leading_literal_are_not_filtered(scan_filter):
    class Event(DynamoModel):
        source: str
        at: str

        class Meta:
            indexes = single_table_indexes(base=("{source}", "EVENT#{at}"))

    assert scan_filter(Event) == "begins_with(SK, 'EVENT#')"


def test_scan_values_are_all_filters(order_model, table, scan_filter):
    rendered = scan_filter(
        order_model, {"customer_id": "c1", "date": Between("2024", "2025")}
    )
    assert "KeyConditionExpression" not in table.scan.call_args.kwargs
    assert rendered == (
        "((begins_with(PK, 'CUSTOMER#') AND begins_with(SK, 'ORDER#')) AND "
        "(customer_id = 'c1' AND date BETWEEN '2024' AND '2025'))"
    )
