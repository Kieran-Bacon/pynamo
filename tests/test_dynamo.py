import unittest
from typing import ClassVar
from unittest.mock import Mock

import pydantic
from boto3.dynamodb.conditions import ConditionExpressionBuilder

from pynamo.dynamo import (
    DynamoModel,
    GlobalSecondaryIndex,
    LocalSecondaryIndex,
    PrimaryIndex,
    begins_with,
    between,
    gt,
    gte,
    lt,
    ne,
    single_table_indexes,
)


class DynamoModelTests(unittest.TestCase):
    def test_index_models_validate_non_empty_templates(self):
        with self.assertRaises(pydantic.ValidationError):
            LocalSecondaryIndex(
                index_name="ByName",
                sk_name="LSI1SK",
                sk_template="",
            )

    def test_factory_sets_primary_and_secondary_indexes(self):
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

        self.assertEqual(
            AuditLog._primary_index,
            PrimaryIndex(
                pk_name="PK",
                sk_name="SK",
                pk_template="AUDIT_LOG#{audit_log_id}",
                sk_template="AUDIT_LOG#{timestamp}",
            ),
        )
        self.assertIsInstance(AuditLog.indexes, list)
        self.assertIsInstance(AuditLog.indexes[0], PrimaryIndex)
        self.assertIsInstance(AuditLog.indexes[1], LocalSecondaryIndex)
        self.assertFalse(hasattr(AuditLog.indexes[1], "pk_name"))

    def test_local_index_defines_only_its_sort_key_template(self):
        indexes = single_table_indexes(
            base=("USER#{user_id}", "USER#{created_at}"),
            local=["USER_BY_NAME#{name}"],
        )

        self.assertIsInstance(indexes, list)
        self.assertIsInstance(indexes[1], LocalSecondaryIndex)
        self.assertEqual(indexes[1].sk_name, "LSI1SK")
        self.assertEqual(indexes[1].sk_template, "USER_BY_NAME#{name}")
        self.assertFalse(hasattr(indexes[1], "pk_name"))

    def test_supports_custom_index_names_and_key_attributes(self):
        index = GlobalSecondaryIndex(
            index_name="ByEmail",
            pk_name="EMAIL_PK",
            sk_name="EMAIL_SK",
            pk_template="EMAIL#{email}",
            sk_template="USER#{user_id}",
        )
        custom_indexes: list[
            PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex
        ] = [
            PrimaryIndex(
                pk_name="PK",
                sk_name="SK",
                pk_template="USER#{user_id}",
                sk_template="USER#{user_id}",
            ),
            index,
        ]

        class EmailUser(DynamoModel):
            indexes: ClassVar[
                list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex]
            ] = custom_indexes

            user_id: str
            email: str

        self.assertIs(EmailUser._primary_index, custom_indexes[0])

    def test_requires_primary_index_when_subclass_is_created(self):
        with self.assertRaisesRegex(ValueError, "must include a PrimaryIndex"):

            class ModelWithoutPrimaryIndex(DynamoModel):
                indexes: ClassVar[
                    list[PrimaryIndex | LocalSecondaryIndex | GlobalSecondaryIndex]
                ] = [
                    LocalSecondaryIndex(
                        index_name="ByName",
                        sk_name="LSI1SK",
                        sk_template="USER#{name}",
                    )
                ]

                name: str

    def test_query_formats_between_bounds_for_the_selected_index(self):
        class AuditLog(DynamoModel):
            indexes = single_table_indexes(
                base=("AUDIT_LOG#{audit_log_id}", "AUDIT_LOG#{timestamp}"),
                global_indexes=[
                    ("AUDIT_LOG_BY_USER#{user_id}", "AUDIT_LOG#{timestamp}")
                ],
            )

            audit_log_id: str
            user_id: str
            timestamp: str

        table = Mock()
        table.query.return_value = {"Items": []}

        pages = list(
            AuditLog.query(
                table,
                index_name="GSI1",
                user_id="user-1",
                timestamp=between("2020", "2022"),
            )
        )

        self.assertEqual(len(pages), 1)
        query_args = table.query.call_args.kwargs
        self.assertEqual(query_args["IndexName"], "GSI1")
        key_expression = query_args["KeyConditionExpression"].get_expression()
        partition_condition, sort_condition = key_expression["values"]
        partition_values = partition_condition.get_expression()["values"]
        self.assertEqual(partition_values[0].name, "GSI1PK")
        self.assertEqual(partition_values[1], "AUDIT_LOG_BY_USER#user-1")
        sort_expression = sort_condition.get_expression()
        self.assertEqual(sort_expression["operator"], "BETWEEN")
        self.assertEqual(sort_expression["values"][0].name, "GSI1SK")
        self.assertEqual(
            sort_expression["values"][1:],
            ("AUDIT_LOG#2020", "AUDIT_LOG#2022"),
        )


def _render(condition, is_key_condition=False):
    """Render a boto3 condition into its expression with names and values inlined"""
    built = ConditionExpressionBuilder().build_expression(
        condition, is_key_condition=is_key_condition
    )
    expression = built.condition_expression
    for placeholder, name in built.attribute_name_placeholders.items():
        expression = expression.replace(placeholder, name)
    # Replace longest placeholders first so :v10 is not clobbered by :v1
    for placeholder in sorted(built.attribute_value_placeholders, key=len, reverse=True):
        value = built.attribute_value_placeholders[placeholder]
        rendered = f"'{value}'" if isinstance(value, str) else str(value)
        expression = expression.replace(placeholder, rendered)
    return expression


class Order(DynamoModel):
    indexes = single_table_indexes(
        base=("CUSTOMER#{customer_id}", "ORDER#{date}#{order_id}"),
        local=["ORDER_BY_STATUS#{status}#{date}"],
        global_indexes=[("ORDER#{order_id}", "ORDER")],
    )

    customer_id: str
    order_id: str
    date: str
    status: str
    total: int


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.table.query.return_value = {"Items": []}

    def query(self, *args, **kwargs):
        list(Order.query(self.table, *args, **kwargs))
        request = self.table.query.call_args.kwargs
        key = _render(request["KeyConditionExpression"], is_key_condition=True)
        filter_expression = request.get("FilterExpression")
        return request, key, filter_expression and _render(filter_expression)

    def test_full_sort_key_is_an_equality(self):
        _, key, filters = self.query(customer_id="c1", date="2024", order_id="o1")
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND SK = 'ORDER#2024#o1')")
        self.assertIsNone(filters)

    def test_missing_sort_key_attributes_match_on_the_fixed_prefix(self):
        _, key, filters = self.query(customer_id="c1")
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))")
        self.assertIsNone(filters)

        _, key, _ = self.query(customer_id="c1", date="2024")
        self.assertEqual(
            key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#2024#'))"
        )

    def test_comparator_on_a_middle_attribute_is_also_filtered(self):
        _, key, filters = self.query(
            customer_id="c1", date=between("2024-01", "2024-03")
        )
        self.assertEqual(
            key,
            "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#2024-01' "
            "AND 'ORDER#2024-03\U0010ffff')",
        )
        self.assertEqual(filters, "date BETWEEN '2024-01' AND '2024-03'")

    def test_second_sort_key_condition_goes_to_the_filter(self):
        _, key, filters = self.query(
            customer_id="c1",
            date=gte("2024"),
            order_id=begins_with("o"),
        )
        self.assertEqual(
            key,
            "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#2024' AND 'ORDER#\U0010ffff')",
        )
        self.assertEqual(filters, "begins_with(order_id, 'o')")

    def test_attribute_after_a_missing_one_goes_to_the_filter(self):
        _, key, filters = self.query(customer_id="c1", order_id="o1")
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))")
        self.assertEqual(filters, "order_id = 'o1'")

    def test_non_key_attributes_are_filtered(self):
        _, _, filters = self.query(customer_id="c1", status="open", total=gt(10))
        self.assertEqual(filters, "(status = 'open' AND total > 10)")

    def test_exclusive_bound_with_a_prefix_is_refined_by_the_filter(self):
        _, key, filters = self.query(customer_id="c1", date=lt("2024"))
        self.assertEqual(
            key, "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#' AND 'ORDER#2024')"
        )
        self.assertEqual(filters, "date < '2024'")

    def test_local_index_uses_the_table_partition_key(self):
        request, key, filters = self.query(
            "LSI1", customer_id="c1", status="open", date=between("2024", "2025")
        )
        self.assertEqual(request["IndexName"], "LSI1")
        self.assertEqual(
            key,
            "(PK = 'CUSTOMER#c1' AND LSI1SK BETWEEN "
            "'ORDER_BY_STATUS#open#2024' AND 'ORDER_BY_STATUS#open#2025')",
        )
        self.assertIsNone(filters)

    def test_filter_only_comparator_on_the_sort_key(self):
        _, key, filters = self.query(customer_id="c1", date=ne("2024"))
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))")
        self.assertEqual(filters, "date <> '2024'")

    def test_partition_key_values_are_required(self):
        with self.assertRaisesRegex(ValueError, "order_id are required"):
            Order.query(self.table, "GSI1", customer_id="c1")
        with self.assertRaisesRegex(ValueError, "must be given as plain values"):
            Order.query(self.table, customer_id=begins_with("c"))

    def test_unknown_attributes_and_indexes_are_rejected(self):
        with self.assertRaisesRegex(TypeError, "colour"):
            Order.query(self.table, customer_id="c1", colour="red")
        with self.assertRaisesRegex(ValueError, "no index named 'GSI9'"):
            Order.query(self.table, "GSI9", customer_id="c1")

    def test_pages_until_the_limit_is_reached(self):
        item = Order(
            customer_id="c1", order_id="o1", date="2024-01-01", status="open", total=1
        ).model_dump()
        self.table.query.side_effect = [
            {"Items": [item], "LastEvaluatedKey": {"PK": "a"}},
            {"Items": [item, item], "LastEvaluatedKey": {"PK": "b"}},
        ]

        pages = list(Order.query(self.table, customer_id="c1", limit=3))

        self.assertEqual([len(page.items) for page in pages], [1, 2])
        self.assertEqual(pages[-1].token, {"PK": "b"})
        self.assertIs(pages[0].items[0]._table, self.table)
        second_request = self.table.query.call_args_list[1].kwargs
        self.assertEqual(second_request["Limit"], 2)
        self.assertEqual(second_request["ExclusiveStartKey"], {"PK": "a"})
