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

        page = next(
            AuditLog.query(
                table,
                {"user_id": "user-1", "timestamp": between("2020", "2022")},
                index_name="GSI1",
            )
        )

        self.assertEqual(page.items, [])
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
    for placeholder in sorted(
        built.attribute_value_placeholders, key=len, reverse=True
    ):
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


def _order_item(**overrides):
    """Get a stored Order item, with any attributes overridden."""
    order = Order(
        customer_id="c1", order_id="o1", date="2024-01-01", status="open", total=1
    )
    return order.model_dump() | overrides


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.table.query.return_value = {"Items": []}

    def query(self, *args, **kwargs):
        next(Order.query(self.table, *args, **kwargs))
        request = self.table.query.call_args.kwargs
        key = _render(request["KeyConditionExpression"], is_key_condition=True)
        filter_expression = request.get("FilterExpression")
        return request, key, filter_expression and _render(filter_expression)

    def test_full_sort_key_is_an_equality(self):
        _, key, filters = self.query(
            {"customer_id": "c1", "date": "2024", "order_id": "o1"}
        )
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND SK = 'ORDER#2024#o1')")
        self.assertIsNone(filters)

    def test_missing_sort_key_attributes_match_on_the_fixed_prefix(self):
        _, key, filters = self.query({"customer_id": "c1"})
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))")
        self.assertIsNone(filters)

        _, key, _ = self.query({"customer_id": "c1", "date": "2024"})
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#2024#'))")

    def test_comparator_on_a_middle_attribute_is_also_filtered(self):
        _, key, filters = self.query(
            {"customer_id": "c1", "date": between("2024-01", "2024-03")}
        )
        self.assertEqual(
            key,
            "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#2024-01' "
            "AND 'ORDER#2024-03\U0010ffff')",
        )
        self.assertEqual(filters, "date BETWEEN '2024-01' AND '2024-03'")

    def test_second_sort_key_condition_goes_to_the_filter(self):
        _, key, filters = self.query(
            {"customer_id": "c1", "date": gte("2024"), "order_id": begins_with("o")}
        )
        self.assertEqual(
            key,
            "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#2024' AND 'ORDER#\U0010ffff')",
        )
        self.assertEqual(filters, "begins_with(order_id, 'o')")

    def test_attribute_after_a_missing_one_goes_to_the_filter(self):
        _, key, filters = self.query({"customer_id": "c1", "order_id": "o1"})
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))")
        self.assertEqual(filters, "order_id = 'o1'")

    def test_non_key_attributes_are_filtered(self):
        _, _, filters = self.query(
            {"customer_id": "c1", "status": "open", "total": gt(10)}
        )
        self.assertEqual(filters, "(status = 'open' AND total > 10)")

    def test_exclusive_bound_with_a_prefix_is_refined_by_the_filter(self):
        _, key, filters = self.query({"customer_id": "c1", "date": lt("2024")})
        self.assertEqual(
            key, "(PK = 'CUSTOMER#c1' AND SK BETWEEN 'ORDER#' AND 'ORDER#2024')"
        )
        self.assertEqual(filters, "date < '2024'")

    def test_local_index_uses_the_table_partition_key(self):
        request, key, filters = self.query(
            {"customer_id": "c1", "status": "open", "date": between("2024", "2025")},
            "LSI1",
        )
        self.assertEqual(request["IndexName"], "LSI1")
        self.assertEqual(
            key,
            "(PK = 'CUSTOMER#c1' AND LSI1SK BETWEEN "
            "'ORDER_BY_STATUS#open#2024' AND 'ORDER_BY_STATUS#open#2025')",
        )
        self.assertIsNone(filters)

    def test_filter_only_comparator_on_the_sort_key(self):
        _, key, filters = self.query({"customer_id": "c1", "date": ne("2024")})
        self.assertEqual(key, "(PK = 'CUSTOMER#c1' AND begins_with(SK, 'ORDER#'))")
        self.assertEqual(filters, "date <> '2024'")

    def test_partition_key_values_are_required(self):
        with self.assertRaisesRegex(ValueError, "order_id are required"):
            next(Order.query(self.table, {"customer_id": "c1"}, "GSI1"))
        with self.assertRaisesRegex(ValueError, "must be given as plain values"):
            next(Order.query(self.table, {"customer_id": begins_with("c")}))

    def test_unknown_attributes_and_indexes_are_rejected(self):
        with self.assertRaisesRegex(TypeError, "colour"):
            next(Order.query(self.table, {"customer_id": "c1", "colour": "red"}))
        with self.assertRaisesRegex(ValueError, "no index named 'GSI9'"):
            next(Order.query(self.table, {"customer_id": "c1"}, "GSI9"))

    def test_attributes_can_share_names_with_query_parameters(self):
        class Setting(DynamoModel):
            indexes = single_table_indexes(base=("TABLE#{table}", "LIMIT#{limit}"))

            table: str
            limit: int
            index_name: str

        next(
            Setting.query(
                self.table,
                {"table": "t1", "limit": gte(5), "index_name": "i1"},
                limit=10,
            )
        )
        request = self.table.query.call_args.kwargs

        self.assertEqual(request["Limit"], 10)
        self.assertNotIn("IndexName", request)
        self.assertEqual(
            _render(request["KeyConditionExpression"], is_key_condition=True),
            "(PK = 'TABLE#t1' AND SK BETWEEN 'LIMIT#5' AND 'LIMIT#\U0010ffff')",
        )
        self.assertEqual(_render(request["FilterExpression"]), "index_name = 'i1'")

    def test_yields_a_page_per_request_until_there_is_no_token(self):
        self.table.query.side_effect = [
            {"Items": [_order_item()], "LastEvaluatedKey": {"PK": "a"}},
            {"Items": [_order_item(), _order_item()]},
        ]

        pages = list(Order.query(self.table, {"customer_id": "c1"}, limit=3))

        # Each request passes the limit straight to DynamoDB and resumes from the last
        first_request, second_request = (
            call.kwargs for call in self.table.query.call_args_list
        )
        self.assertEqual(first_request["Limit"], 3)
        self.assertNotIn("ExclusiveStartKey", first_request)
        self.assertEqual(second_request["Limit"], 3)
        self.assertEqual(second_request["ExclusiveStartKey"], {"PK": "a"})

        # Items are attached models, and the last page has no token
        self.assertEqual([len(page.items) for page in pages], [1, 2])
        self.assertIs(pages[0].items[0]._table, self.table)
        self.assertEqual([page.token for page in pages], [{"PK": "a"}, None])

    def test_invalid_items_are_collected_on_the_page(self):
        invalid = _order_item(total="lots")
        self.table.query.return_value = {"Items": [_order_item(), invalid]}

        page = next(Order.query(self.table, {"customer_id": "c1"}))

        # The valid item is still returned alongside the failure
        self.assertEqual(len(page.items), 1)
        self.assertEqual(len(page.errors), 1)
        self.assertEqual(page.errors[0].item, invalid)
        self.assertEqual(page.errors[0].error.errors()[0]["loc"], ("total",))


class PutTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.order = Order.model_validate(_order_item())

    def test_writes_the_attributes_and_every_index_key(self):
        self.order.put(self.table)

        self.assertEqual(
            self.table.put_item.call_args.kwargs["Item"],
            _order_item()
            | {
                "PK": "CUSTOMER#c1",
                "SK": "ORDER#2024-01-01#o1",
                "LSI1SK": "ORDER_BY_STATUS#open#2024-01-01",
                "GSI1PK": "ORDER#o1",
                "GSI1SK": "ORDER",
            },
        )

    def test_putting_to_the_attached_table_checkpoints(self):
        self.order.attach(self.table).put()
        self.assertEqual(
            self.order._checkpoint, self.table.put_item.call_args.kwargs["Item"]
        )

    def test_putting_to_another_table_is_a_one_off(self):
        # An unattached model stays unattached and unchecked
        self.order.put(self.table)
        self.assertIsNone(self.order._table)
        self.assertIsNone(self.order._checkpoint)

        # An attached model keeps its table and checkpoint
        self.order.attach(self.table).put()
        checkpoint = self.order._checkpoint
        other = Mock()
        self.order.status = "shipped"
        self.order.put(other)
        other.put_item.assert_called_once()
        self.assertIs(self.order._table, self.table)
        self.assertEqual(self.order._checkpoint, checkpoint)

        # So the attached table is still updated with the change
        self.order.put()
        self.table.update_item.assert_called_once()

    def test_defaults_to_the_attached_table(self):
        self.order.attach(self.table).put()
        self.table.put_item.assert_called_once()

        with self.assertRaisesRegex(ValueError, "not attached to a table"):
            Order.model_validate(_order_item()).put()

    def test_key_templates_can_use_properties(self):
        class Event(DynamoModel):
            indexes = single_table_indexes(base=("SOURCE#{source}", "EVENT#{day}"))

            source: str
            at: str

            @property
            def day(self) -> str:
                return self.at[:10]

        Event(source="s1", at="2024-01-01T09:00").put(self.table)
        item = self.table.put_item.call_args.kwargs["Item"]
        self.assertEqual((item["PK"], item["SK"]), ("SOURCE#s1", "EVENT#2024-01-01"))

    def test_get_reads_back_what_put_wrote(self):
        self.order.attach(self.table).put()
        self.table.get_item.return_value = {
            "Item": self.table.put_item.call_args.kwargs["Item"]
        }

        read = Order.get(self.table, customer_id="c1", date="2024-01-01", order_id="o1")
        self.assertEqual(read, self.order)

    def test_a_checkpointed_model_only_updates_what_changed(self):
        self.order.attach(self.table).put()
        self.order.status = "shipped"
        self.order.total = 2
        self.order.put()

        # Only the changed attributes, and the index key built from status, are set
        self.table.put_item.assert_called_once()
        arguments = self.table.update_item.call_args.kwargs
        self.assertEqual(
            arguments["Key"], {"PK": "CUSTOMER#c1", "SK": "ORDER#2024-01-01#o1"}
        )
        self.assertEqual(
            arguments["UpdateExpression"], "SET #u0 = :u0, #u1 = :u1, #u2 = :u2"
        )
        self.assertEqual(
            arguments["ExpressionAttributeNames"],
            {"#u0": "status", "#u1": "total", "#u2": "LSI1SK"},
        )
        self.assertEqual(
            arguments["ExpressionAttributeValues"],
            {":u0": "shipped", ":u1": 2, ":u2": "ORDER_BY_STATUS#shipped#2024-01-01"},
        )
        self.assertEqual(
            _render(arguments["ConditionExpression"]), "attribute_exists(PK)"
        )

        # The new state is checkpointed, so putting again sends nothing
        self.order.put()
        self.table.update_item.assert_called_once()

    def test_a_detached_model_is_put_whole(self):
        self.order.attach(self.table).put()
        self.order.attach(None)
        self.order.put(self.table)
        self.assertEqual(self.table.put_item.call_count, 2)
        self.table.update_item.assert_not_called()

    def test_changing_the_primary_key_is_rejected(self):
        self.order.attach(self.table).put()
        self.order.order_id = "o2"
        with self.assertRaisesRegex(ValueError, "primary key has changed"):
            self.order.put()


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.table.get_item.return_value = {"Item": _order_item()}

    def get(self):
        """Read the order, so it is attached and checkpointed."""
        return Order.get(self.table, customer_id="c1", date="2024-01-01", order_id="o1")

    def test_deletes_by_the_primary_key(self):
        Order.model_validate(_order_item()).delete(self.table)
        self.table.delete_item.assert_called_once_with(
            Key={"PK": "CUSTOMER#c1", "SK": "ORDER#2024-01-01#o1"}
        )

    def test_uses_the_checkpointed_key_when_the_key_has_changed(self):
        order = self.get()
        order.order_id = "o2"
        order.delete()
        self.table.delete_item.assert_called_once_with(
            Key={"PK": "CUSTOMER#c1", "SK": "ORDER#2024-01-01#o1"}
        )

    def test_drops_the_checkpoint_but_stays_attached(self):
        order = self.get().delete()
        self.assertIsNone(order._checkpoint)
        self.assertIs(order._table, self.table)

    def test_deleting_from_another_table_keeps_the_checkpoint(self):
        order = self.get()
        checkpoint = order._checkpoint
        other = Mock()
        order.delete(other)
        other.delete_item.assert_called_once()
        self.assertEqual(order._checkpoint, checkpoint)

    def test_requires_a_table(self):
        with self.assertRaisesRegex(ValueError, "not attached to a table"):
            Order.model_validate(_order_item()).delete()


class RemoveTests(unittest.TestCase):
    def test_deletes_by_the_key_built_from_the_values(self):
        table = Mock()
        Order.remove(table, customer_id="c1", date="2024-01-01", order_id="o1")
        table.delete_item.assert_called_once_with(
            Key={"PK": "CUSTOMER#c1", "SK": "ORDER#2024-01-01#o1"}
        )

    def test_get_and_remove_accept_a_key_attribute_named_table(self):
        class Setting(DynamoModel):
            indexes = single_table_indexes(base=("TABLE#{table}", "SETTING"))

            table: str

        table = Mock()
        table.get_item.return_value = {"Item": {"table": "t1"}}
        key = {"PK": "TABLE#t1", "SK": "SETTING"}

        self.assertEqual(Setting.get(table, table="t1").table, "t1")
        table.get_item.assert_called_once_with(Key=key)
        Setting.remove(table, table="t1")
        table.delete_item.assert_called_once_with(Key=key)

    def test_key_attributes_are_required(self):
        table = Mock()
        with self.assertRaises(KeyError):
            Order.remove(table, customer_id="c1", date="2024-01-01")
        table.delete_item.assert_not_called()


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.table.scan.return_value = {"Items": []}

    def scan(self, *args, **kwargs):
        next(Order.scan(self.table, *args, **kwargs))
        return self.table.scan.call_args.kwargs

    def test_without_values_items_are_matched_by_primary_key_prefixes(self):
        request = self.scan()
        self.assertEqual(
            _render(request["FilterExpression"]),
            "(begins_with(PK, 'CUSTOMER#') AND begins_with(SK, 'ORDER#'))",
        )

    def test_keys_without_a_leading_literal_are_not_filtered(self):
        class Event(DynamoModel):
            indexes = single_table_indexes(base=("{source}", "EVENT#{at}"))

            source: str
            at: str

        next(Event.scan(self.table))
        request = self.table.scan.call_args.kwargs
        self.assertEqual(
            _render(request["FilterExpression"]), "begins_with(SK, 'EVENT#')"
        )

    def test_every_value_is_a_filter(self):
        request = self.scan({"customer_id": "c1", "date": between("2024", "2025")})
        self.assertNotIn("KeyConditionExpression", request)
        self.assertEqual(
            _render(request["FilterExpression"]),
            "((begins_with(PK, 'CUSTOMER#') AND begins_with(SK, 'ORDER#')) AND "
            "(customer_id = 'c1' AND date BETWEEN '2024' AND '2025'))",
        )

    def test_request_options_are_passed_to_dynamodb(self):
        request = self.scan(
            {"status": "open"},
            "GSI1",
            limit=5,
            consistent_read=True,
            start_token={"PK": "a"},
        )
        self.assertEqual(request["IndexName"], "GSI1")
        self.assertEqual(request["Limit"], 5)
        self.assertTrue(request["ConsistentRead"])
        self.assertEqual(request["ExclusiveStartKey"], {"PK": "a"})

    def test_unknown_attributes_and_indexes_are_rejected(self):
        with self.assertRaisesRegex(TypeError, "colour"):
            next(Order.scan(self.table, {"colour": "red"}))
        with self.assertRaisesRegex(ValueError, "no index named 'GSI9'"):
            next(Order.scan(self.table, index_name="GSI9"))
        with self.assertRaisesRegex(ValueError, "limit must be a positive integer"):
            next(Order.scan(self.table, limit=0))

    def test_yields_pages_and_collects_invalid_items(self):
        invalid = _order_item(total="lots")
        self.table.scan.side_effect = [
            {"Items": [_order_item()], "LastEvaluatedKey": {"PK": "a"}},
            {"Items": [_order_item(), invalid]},
        ]

        pages = list(Order.scan(self.table))

        # The second request resumes from the first page's token
        second_request = self.table.scan.call_args_list[1].kwargs
        self.assertEqual(second_request["ExclusiveStartKey"], {"PK": "a"})

        # Valid items are attached models; invalid ones are on the page's errors
        self.assertEqual([len(page.items) for page in pages], [1, 1])
        self.assertIs(pages[0].items[0]._table, self.table)
        self.assertEqual([page.errors[0].item for page in pages[1:]], [invalid])
        self.assertEqual([page.token for page in pages], [{"PK": "a"}, None])


class TemplateFieldTests(unittest.TestCase):
    """Key template fields must name top-level attributes of the model.

    Nested fields such as `{owner.id}` are deliberately unsupported (DynamoDB does not
    need them, and supporting them would mean matching values by attribute path
    throughout), so they are rejected when the index is defined.
    """

    def test_nested_template_fields_are_rejected(self):
        for template in ("OWNER#{owner.id}", "TAG#{tags[0]}", "ITEM#{}", "ITEM#{0}"):
            with (
                self.subTest(template=template),
                self.assertRaisesRegex(pydantic.ValidationError, "top-level"),
            ):
                single_table_indexes(base=(template, "ITEM"))

    def test_format_specs_and_conversions_are_allowed(self):
        index = LocalSecondaryIndex(
            index_name="LSI1", sk_name="LSI1SK", sk_template="AT#{timestamp:010d}{id!s}"
        )
        self.assertEqual(index.sk_template, "AT#{timestamp:010d}{id!s}")
