import contextlib
import datetime
import decimal
import enum
import logging
import typing
from typing import Any, Generic, TypeVar

import arrow
import boto3
import botocore
import cachetools
import numpy as np
import pydantic
from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from mypy_boto3_dynamodb.literals import SelectType
from mypy_boto3_dynamodb.service_resource import Table
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    model_validator,
)

from .utils import build_set_statement, generate_change_dict

log = logging.getLogger(__name__)

T = typing.TypeVar("T", bound=BaseModel)


class PydanticDynamoDBClient(Generic[T]):
    """A DynamoDB client that handles getting and putting items from pydantic models as well as providing caching
    of items to avoid multiple calls

    Params:
        table (str) the name of the dynamoDB table being connected to
        data_model (pydantic.BaseModel): Class object the item is to be unpacked into
        *,
        aws_session (boto3.Session) - None: The session object to use when connecting to dynamoDB
    """

    Key = Key
    Attr = Attr

    def __init__(
        self,
        table: Table,
        data_model: type[T],
        *,
        aws_session: boto3.Session | None = None,
    ):

        self._aws_session = aws_session or boto3.Session()
        self._data_model = data_model
        self._tablename = table.name
        self._table = table
        self._batch = None  # Hold batch writer and reader when called

        self.validation_errors: list[dict] = []

    @property
    def tablename(self) -> str:
        return self._tablename

    @property
    def table(self) -> Table:
        return self._table

    def get(self, partition_key: str, sort_key: str | None = None) -> T:

        # key = {name: value for name, value in zip(self._index_keys, (partition_key, sort_key)) if value is not None}
        key = {"PK": partition_key, "SK": sort_key}

        try:
            response = self._table.get_item(Key=key)
            new_obj = self._data_model.model_validate(response["Item"])

            if isinstance(new_obj, DynamoDBConnectedBaseModel):
                new_obj.attach(self._table)
                new_obj.checkpoint()

            return new_obj

        except botocore.exceptions.ClientError as exc:  # type: ignore
            raise ValueError(
                f"Validation error on table={self._tablename} key={key} - {exc.response['Error']['Code']} {exc.response['Error']['Message']}"
            ) from exc

        except KeyError as exc:
            raise KeyError(
                f"Could not find entry for key {key} in dynamo table {self._tablename}"
            ) from exc

    def scan(self, *args, **kwargs) -> list[T]:
        """Scan the table and return all models of that type

        e.g.

            return client.scan(
                FilterExpression=Key('PK').begins_with('USER') & Key('SK').begins_with('METRIC'),
            )


        """

        models = []
        validation_errors = []
        while True:
            response = self._table.scan(*args, **kwargs)

            for model_data in response["Items"]:
                try:
                    new_obj = self._data_model.model_validate(model_data)

                    if isinstance(new_obj, DynamoDBConnectedBaseModel):
                        new_obj.attach(self._table)

                    models.append(new_obj)

                except pydantic.ValidationError:
                    log.exception(
                        "Experienced validation error trying to parse PK=%s SK=%s into %s",
                        model_data["PK"],
                        model_data["SK"],
                        self._data_model,
                    )
                    validation_errors.append(model_data)

            if "LastEvaluatedKey" not in response:
                break

            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        self.validation_errors = validation_errors
        return models

    def tokenQuery(
        self,
        Select: SelectType = "ALL_ATTRIBUTES",
        pagination_token: str | None = None,
        **kwargs,
    ) -> tuple[dict[str, str] | None, list[T]]:

        # Allow users to pass values that should be treated as Optional
        if "Limit" in kwargs and kwargs["Limit"] is None:
            kwargs.pop("Limit")

        if "ExclusiveStartKey" in kwargs and kwargs["ExclusiveStartKey"] is None:
            kwargs.pop("ExclusiveStartKey")

        if pagination_token is not None:
            kwargs["ExclusiveStartKey"] = json.loads(pagination_token)

        # Perform the query
        response = self._table.query(Select=Select, **kwargs)

        # Iterate over the models and
        models = []
        for model_data in response["Items"]:
            try:
                new_obj = self._data_model.model_validate(model_data)

                if isinstance(new_obj, DynamoDBConnectedBaseModel):
                    new_obj.attach(self._table)
                    new_obj.checkpoint()

                models.append(new_obj)

            except pydantic.ValidationError:
                log.exception(
                    "Experienced validation error trying to parse PK=%s SK=%s into %s",
                    model_data["PK"],
                    model_data["SK"],
                    self._data_model,
                )
                self.validation_errors.append(model_data)

        return response.get("LastEvaluatedKey"), models  # type: ignore

    def query(
        self,
        partition_key: str | None = None,
        KeyConditionExpression: ConditionBase | None = None,
        Select: SelectType = "ALL_ATTRIBUTES",
        Limit: int | None = None,
        **kwargs,
    ) -> list[T]:
        """Query the dynamo DB table - all params to boto3.Table.query are valid

        E.g.

            KeyConditionExpression = Key('PK').eq(partition_key)

        Args:
            partition_key (Optional[str], optional): Short had for the key condition to check all of a partition. Defaults to None.
            KeyConditionExpression (Optional[ConditionBase]): The dynamo key expression to match with the documents. e.g. Key('PK').eq('USERS')
            Select (SelectType, optional): Default overload of boto3 for query to return all attributes. Defaults to "ALL_ATTRIBUTES".

        Returns:
            List[T]: _description_
        """

        if partition_key is not None:
            kwargs["KeyConditionExpression"] = Key("PK").eq(partition_key)

        elif KeyConditionExpression is not None:
            kwargs["KeyConditionExpression"] = KeyConditionExpression

        if Limit is not None:
            kwargs["Limit"] = Limit

        models = []
        self.validation_errors = []
        while True:
            # Query the table - extend the return list with the result
            token, models_page = self.tokenQuery(Select=Select, **kwargs)
            models.extend(models_page)

            if token is not None:
                # Token implies that there is more to return for the query provided
                # update the query arguments to continue the query and continue onto the next loop
                kwargs["ExclusiveStartKey"] = token

                # If a limit was set - reduce the limit number to the remaining models
                limit = kwargs.get("Limit")
                if limit is not None:
                    limit -= len(models_page)
                    if limit:
                        kwargs["Limit"] = limit
                    else:
                        # There are no more things to return
                        break

                continue

            break

        return models

    @classmethod
    def _sanitiseItem(cls, item: dict[Any, Any]) -> dict[str, typing.Any]:

        for key, value in item.items():
            if isinstance(value, dict):
                item[key] = cls._sanitiseItem(value)

            elif isinstance(value, enum.Enum):
                item[key] = value.value

            elif isinstance(value, np.ndarray):
                if value.shape == ():
                    # This a single value held in an array
                    val = value.tolist()
                    if isinstance(val, float):
                        val = decimal.Decimal(str(value))
                    item[key] = val

                else:
                    item[key] = list(
                        cls._sanitiseItem(
                            {i: v for i, v in enumerate(value.tolist())}
                        ).values()
                    )

            elif isinstance(value, (list, set)):
                # NOTE relying on the order of dictionaries
                # https://medium.com/junior-dev/python-dictionaries-are-ordered-now-but-how-and-why-5d5a40ee327f
                item[key] = list(
                    cls._sanitiseItem({i: v for i, v in enumerate(value)}).values()
                )

            elif isinstance(value, float):
                item[key] = decimal.Decimal(str(value))

            elif isinstance(value, datetime.datetime):
                item[key] = value.isoformat()

            elif isinstance(value, arrow.Arrow):
                item[key] = value.datetime.isoformat()

        return item

    @classmethod
    def sanitise(cls, item: T | pydantic.BaseModel) -> dict[str, typing.Any]:
        """Convert the model provided (not generic specific) and convert it to a valid dynamo item

        Params:
            item (Union[T, pydantic.BaseModel]): The model to sanitise
        """

        item_data = item.model_dump()

        selected_model_properties = {"PK", "SK"}.union(
            set(getattr(item, "_properties", []))
        )
        for selected_property in selected_model_properties:
            try:
                item_data[selected_property] = getattr(item, selected_property)
            except:
                raise RuntimeError(
                    f"Failed to extract model property {selected_property} from {item}"
                )

        # get the value of any class properties
        # for prop, val in  [(p, getattr(item,p)) for p in dir(item) if not p.startswith('_') and isinstance(getattr(item,p),property)]:
        #     item_data[prop] = val()

        return cls._sanitiseItem(item_data)

    def put(self, item: T):
        assert isinstance(item, self._data_model), (
            f"{item} is not type {self._data_model}"
        )

        writer = self._batch if self._batch is not None else self._table

        return writer.put_item(Item=self.sanitise(item))

    def delete(self, PK: str, SK: str):

        writer = self._batch if self._batch is not None else self._table

        return writer.delete_item(Key={"PK": PK, "SK": SK})

    @contextlib.contextmanager
    def batch_writer(self):
        with self.table.batch_writer() as batch:
            self._batch = batch
            yield self


class PydanticDynamoDBCacheClient(PydanticDynamoDBClient, Generic[T]):
    """A DynamoDB client that handles getting and putting items from pydantic models as well as providing caching
    of items to avoid multiple calls

    Args:
        table (str) the name of the dynamoDB table being connected to
        data_model (pydantic.BaseModel): Class object the item is to be unpacked into
        ttl (float): The time to live for the item in the internal cache
        *,
        aws_session (boto3.Session) - None: The session object to use when connecting to dynamoDB
        on_update_callback (typing.Callable[[pydantic.BaseModel], None]) = None: A callback to run when an item is newly
            downloaded as opposed to being fetched from the cache.
    """

    def __init__(
        self,
        table: Table,
        data_model: type[T],
        ttl: float,
        *,
        aws_session: boto3.Session | None = None,
        on_update_callback: typing.Callable[[T], None] | None = None,
    ):

        super().__init__(table, data_model, aws_session=aws_session)

        # self._index_keys = [key['AttributeName'] for key in self._table.key_schema]
        self._cache = cachetools.TTLCache(maxsize=1000, ttl=ttl)
        self._ttl = ttl
        self._on_update_callback = on_update_callback

    @staticmethod
    def _hash_key(partition_key: str, sort_key: str | None = None):
        """Turn dynamo key object into a python dictionary hashable key"""
        return hash(
            "".join((partition_key, sort_key))
            if sort_key is not None
            else partition_key
        )

    def get(self, partition_key: str, sort_key: str | None = None) -> T:

        hashed_key = self._hash_key(partition_key, sort_key)

        try:
            return self._cache[hashed_key]

        except KeyError:
            self._cache[hashed_key] = model = super().get(partition_key, sort_key)

            if self._on_update_callback is not None:
                self._on_update_callback(model)

            return model

    def put(self, item: T):
        super().put(item)
        # self._cache[self._hash_key(*[getattr(item, key) for key in self._index_keys])] = item
        self._cache[self._hash_key(item.PK, item.SK)] = item

    def seed_cache(self, data: list[dict]):
        """Seed the cache with values not fetched from dynamo"""

        for item in data:
            try:
                model = self._data_model.parse_obj(item)
            except Exception as e:
                raise ValueError(
                    f"Failed to parse data item {item} as {self._data_model}"
                ) from e

            # self._cache[self._hash_key(*[item[key] for key in self._index_keys])] = model
            self._cache[self._hash_key(item["PK"], item["SK"])] = model


class ModelNotAttachedError(Exception):
    """Raised when a model is not attached to a dynamoDB table"""


class class_or_instancemethod(classmethod):
    def __get__(self, instance, type_):
        descr_get = super().__get__ if instance is None else self.__func__.__get__
        return descr_get(instance, type_)


D = typing.TypeVar("D", bound="DynamoDBConnectedBaseModel")


# Unfortunately class variables are looked up by children so you cannot save the client on the base model as
# child models will have a client that will yield parent classes.
DYNAMO_CLIENTS: dict[type, PydanticDynamoDBClient] = {}


def dynamo_client_get(modelClass: type, dynamo_table) -> PydanticDynamoDBClient | None:
    if modelClass in DYNAMO_CLIENTS:
        client = DYNAMO_CLIENTS[modelClass]
        if dynamo_table is not None and client.table == dynamo_table:
            return client


# class DynamoDBConnectedBaseModel(Generic[T]):
class DynamoDBConnectedBaseModel(pydantic.BaseModel):
    """A pydantic model that has been connected to a dynamoDB table"""

    _properties = ["PK", "SK"]
    _dynamo_keys = ["PK", "SK"]
    # dynamo_table: Optional[Table] = pydantic.Field(None, exclude=True)

    _checkpoint = None

    # TODO implementation doesn't work
    # def __setattr__(self, key, value):
    #     # Required to allow the use of property setters
    #     # https://github.com/pydantic/pydantic/issues/1577

    #     try:
    #         super().__setattr__(key, value)
    #     except:
    #         field = getattr(self.__class__, key)
    #         if isinstance(field, property) and field.fset is not None:
    #             field.__setattr__(key, value)
    #         else:
    #             raise

    @model_validator(mode="wrap")
    @classmethod
    def check_dynamo_keys(
        cls, data: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ):

        if isinstance(data, cls):
            obj = handler(data)
            return obj

        try:
            index_vals = {idx: data.get(idx, None) for idx in cls._dynamo_keys.default}  # type: ignore
        except Exception:
            raise ValueError(f"Model must contain keys {cls._dynamo_keys}")

        obj = handler(data)

        for idx, val in index_vals.items():
            new_val = getattr(obj, idx)
            if val is not None and val != new_val:
                raise ValueError(
                    f"Index [{idx}] value has changed from [{val}] to [{new_val}] after model validate"
                )

        return obj

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True,
        json_encoders={
            datetime.datetime: lambda dt: dt.isoformat(),
            datetime.date: lambda dt: dt.isoformat(),
            decimal.Decimal: lambda d: str(d),
            arrow.Arrow: lambda dt: dt.isoformat(),
        },
    )

    @property
    def PK(self) -> str:
        raise NotImplementedError(
            "All subclasses of `DynamoDBConnectedBaseModel` must implement a PK property"
        )

    @property
    def SK(self) -> str:
        raise NotImplementedError(
            "All subclasses of `DynamoDBConnectedBaseModel` must implement a SK property"
        )

    def attach(self, dynamo_table: Table | None) -> typing.Self:
        # def attach(self: 'DynamoDBConnectedBaseModel', dynamo_table: Table) -> 'DynamoDBConnectedBaseModel':
        """Attach this model to a dynamoDB table"""
        self._dynamo_table = dynamo_table

        if dynamo_table is None:
            self._checkpoint = None

        # TODO - to allow nested structures the ability to resolve properties
        # for field_name, field_info in self.model_fields.items():
        #     value = self[field_name]
        #     if isinstance(value, DynamoDBConnectedBaseModel):
        #         value.attach(self._dynamo_table)

        return self

    @class_or_instancemethod
    def client(
        cls, dynamo_table: Table | None = None
    ) -> PydanticDynamoDBClient[typing.Self]:
        """Get a client for this model"""

        if isinstance(cls, DynamoDBConnectedBaseModel):
            client = dynamo_client_get(cls.__class__, dynamo_table)
            if client is not None:
                return client

            if dynamo_table is not None:
                cls._dynamo_table = dynamo_table

            if cls._dynamo_table is None:
                raise ModelNotAttachedError(
                    f"Model {cls.__class__.__name__} is not attached to a dynamoDB table"
                )

            DYNAMO_CLIENTS[cls.__class__] = client = PydanticDynamoDBClient(
                cls._dynamo_table, cls.__class__
            )
            return client

        else:
            client = dynamo_client_get(cls, dynamo_table)
            if client is not None:
                return client

            if dynamo_table is None:
                raise ValueError("dynamo_table must be specified")

            DYNAMO_CLIENTS[cls] = client = PydanticDynamoDBClient(dynamo_table, cls)
            return client

    def put(self, dynamo_table: Table | None = None):
        self.client(dynamo_table or self._dynamo_table).put(self)
        self.checkpoint()

    @classmethod
    def delete(cls, dynamo_table: Table, *args, **kwargs):
        """Delete model of this type as identified by *args and **kwargs"""
        raise NotImplementedError(
            f"delete method not implemented for dynamo model {cls}"
        )

    def remove(self, dynamo_table=None):
        """Delete this instance model"""
        dynamo_table = dynamo_table or self._dynamo_table
        self.client(dynamo_table).delete(self.PK, self.SK)

    def has_checkpoint(self) -> bool:
        return self._checkpoint is not None

    def checkpoint(self):
        """Create a new checkpoint for recording updates against

        Records the current state of the object as the checkpoint against which future changes will be compared in order to determine
        how `update()` statements will be issued to the database
        """

        self._checkpoint = self.model_dump()

    def update(self, dynamo_table: Table | None = None):
        """Write field level changes to the database as an `update` statement"""

        if self._checkpoint is None:
            raise ValueError(
                "No checkpoint detected from which to calculate an update statement"
            )

        dynamo_table = dynamo_table or self._dynamo_table
        if dynamo_table is None:
            raise ModelNotAttachedError("Model is not attached to a dynamoDB table")

        change_dict = generate_change_dict(self._checkpoint, self.model_dump())

        if change_dict:
            expression, names, values = build_set_statement(change_dict)

            self._dynamo_table.update_item(
                Key={"PK": self.PK, "SK": self.SK},
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )

            self.checkpoint()


import json

T = TypeVar("T")


class PaginatedResult(BaseModel, Generic[T]):
    num: int
    items: list[T]
    pagination_token: str | None

    @classmethod
    def create(cls, items: list[T], pagination_data: dict | None = None):
        return cls(
            num=len(items),
            items=items,
            pagination_token=json.dumps(pagination_data) if pagination_data else None,
        )
