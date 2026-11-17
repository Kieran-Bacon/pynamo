import datetime
import enum
import inspect
import logging
import re
import typing
from collections.abc import Callable, Iterable, Mapping, MutableSet
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Literal,
    Optional,
    Type,
    TypeVar,
    Union,
    get_origin,
)

import arrow
import graphql
import simplejson as json
from graphql import (
    DocumentNode,
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLEnumType,
    GraphQLField,
    GraphQLFieldResolver,
    GraphQLFloat,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInt,
    GraphQLList,
    GraphQLNamedOutputType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    GraphQLType,
    GraphQLUnionType,
)
from pydantic import BaseModel
from pydantic.fields import (
    FieldInfo,
)

from .exceptions import (
    ForwardRefException,
    GraphQLMismatchedInputOutputType,
    GraphQLSchemaUnrecognisedTypeError,
)
from .transformers import (
    BaseTransformer,
    IdentityTransformer,
    SnakeToCamelCaseTransformer,
    recursively_apply,
)
from .utils import sanitiseTypeNames

logger = logging.getLogger(__name__)

PydanticModel = TypeVar("PydanticModel", bound=BaseModel)


# class RegisteredModelProperty(BaseModel):
#     func: Callable
#     name: str
#     alias: str
#     return_type: Type


@dataclass
class RegisteredModel:
    model: type[BaseModel]
    name: str
    description: str | None
    type: Literal["output", "input"]

    router: GraphQLRouter

    exclude_fields: list[str] = field(default_factory=list)
    field_transformer: Callable = lambda x: x

    def __post_init__(self) -> None:

        self._properties = {
            self.field_transformer(prop_name): prop
            for prop_name, prop in inspect.getmembers(
                self.model, lambda o: isinstance(o, property)
            )
        }

        self._attributes = {
            self.field_transformer(attr_name): attr.annotation
            for attr_name, attr in self.model.model_fields.items()
        }

        self.exclude_fields = [
            self.field_transformer(name)
            for name in self.exclude_fields
            + ["model_computed_fields", "model_extra", "model_fields_set"]
        ]

    def resolve(self, field_name):

        if field_name in self.model.model_fields:
            return getattr(self.model, field_name)

        if field_name in self._properties:
            return self._properties[field_name].fget()

        raise AttributeError(
            f"Model {self.name} does not have a field or property named {field_name}"
        )

    def _field_filter(self, fields: dict[str, type]):

        _fields = {}

        for field_name, field_type in fields.items():
            if field_name in self.exclude_fields or field_name.startswith("_"):
                # skip pydantic internal properties
                continue

            if field_type == inspect.Signature.empty:
                logger.warning(
                    f"Skipping GraphQL schema generation for {field_name} on model {self.name} as it has an empty signature"
                )
                continue

            # if get_origin(field_type) == dict:
            #     # arbitrary dictionaries cannot be included in GraphQL schemas :: SKIP
            #     logger.warning(f"Skipping GraphQL schema generation for {field_name} on model {self.name} as it is an arbitrary dictionary")
            #     continue

            _fields[field_name] = field_type

        return _fields

    @property
    def properties(self):

        return self._field_filter(
            {
                prop_name: inspect.signature(prop.fget).return_annotation
                for prop_name, prop in self._properties.items()
            }
        )

    @property
    def fields(self):

        return self._field_filter({**self._attributes, **self.properties})

    @property
    def gql_type(self) -> GraphQLObjectType | GraphQLInputObjectType:

        def resolveFields(eval_forward_ref: bool = True):

            gql_fields = {}
            for name, _type in self.fields.items():
                try:
                    gql_type = self.router.get_gql_type(
                        _type,
                        input=self.type == "input",
                        eval_forward_refs=eval_forward_ref,
                    )

                except GraphQLSchemaUnrecognisedTypeError, TypeError:
                    raise GraphQLSchemaUnrecognisedTypeError(
                        f"Model [{self.name}] contains an unsupported type [{_type}] for field [{name}]"
                    )

                try:
                    if self.type == "input":
                        gql_fields[name] = GraphQLInputField(gql_type)
                        continue

                    gql_fields[name] = GraphQLField(gql_type)
                except TypeError:
                    raise GraphQLMismatchedInputOutputType(
                        f"Model [{self.name}] is of type [{self.type}] but field [{name}] is of type [{gql_type!r}]"
                    )

            return gql_fields

        try:
            gql_fields = resolveFields(False)

        except ForwardRefException:
            gql_fields = resolveFields

        gql_type = GraphQLObjectType if self.type != "input" else GraphQLInputObjectType

        return gql_type(
            name=sanitiseTypeNames(self.name),
            fields=gql_fields,
            description=self.description,
        )


class WrappedClass:
    def __init__(self, wrapped_class: BaseModel, field_transformer: Callable) -> None:
        self._wrapped_class = wrapped_class
        self._field_transformer = field_transformer

        self._properties_map = {
            field_transformer(prop_name): prop_name
            for prop_name, prop in inspect.getmembers(
                self._wrapped_class.__class__, lambda o: isinstance(o, property)
            )
        }

        self._attributes_map = {
            field_transformer(attr_name): attr_name
            for attr_name, attr in self._wrapped_class.model_fields.items()
        }

    def __getattr__(self, name: str) -> Any:

        if name in self._properties_map:
            return_value = getattr(self._wrapped_class, self._properties_map[name])
        elif name in self._attributes_map:
            return_value = getattr(self._wrapped_class, self._attributes_map[name])
        else:
            raise AttributeError(
                f"Model [{self._wrapped_class.__name__}] does not have a field or property named [{name}]"
            )

        if isinstance(return_value, BaseModel):
            return WrappedClass(return_value, self._field_transformer)

        if isinstance(return_value, Mapping):
            return return_value

        if isinstance(return_value, (list, MutableSet, frozenset)):
            return [
                (
                    WrappedClass(x, self._field_transformer)
                    if isinstance(x, BaseModel)
                    else x
                )
                for x in return_value
            ]

        return return_value


@dataclass
class RegisteredFunction:
    func: Callable
    type: Literal["query", "mutation"]
    name: str
    description: str | None

    router: GraphQLRouter

    transformer: BaseTransformer
    argument_transformer: Callable = lambda x: x
    field_transformer: Callable = lambda x: x
    deprecation_warning: str | None = None

    def __post_init__(self) -> None:

        self._arg_key_map = {}
        self._arg_val_map = {}
        for argName, argType in inspect.signature(self.func).parameters.items():
            transformedName = self.argument_transformer(argName)
            self._arg_key_map[transformedName] = argName
            self._arg_val_map[transformedName] = argType._annotation

    def _pythonTypes(self, return_type: Any, value: Any):
        """Evaluate the value provided and convert into python types"""

        # TODO handle type containers properly

        typeAnnotation = get_origin(return_type)
        if typeAnnotation:
            # The value is a typing instance and should be handled accordingly

            if typeAnnotation == Union:
                # NOTE this would be handled by calling _pythonType again but since its a common case it is *believed* to be would be faster
                if value is None:
                    if type(value) in return_type.__args__:
                        return value
                    else:
                        raise ValueError("Required argument not provided")

                for _type in return_type.__args__:
                    try:
                        return self._pythonTypes(_type, value)
                    except:
                        pass
                raise TypeError(
                    f"Provided argument value={value} didn't match signature type={return_type}"
                )

            elif typeAnnotation == dict:
                return json.loads(value)

        if issubclass(return_type, BaseModel):
            return return_type.model_validate(
                self.transformer.applyTransformerRecursively(
                    self.transformer.inverseFieldTransformer, value
                )
            )

        if issubclass(return_type, (list, MutableSet)):
            return [self._pythonTypes(return_type.__args__[0], x) for x in value]

        if issubclass(return_type, enum.Enum):
            try:
                return return_type(value)
            except ValueError as e:
                raise TypeError(
                    "Provided argument={value} is not a valid enum value for {return_type}"
                ) from e

        if issubclass(return_type, arrow.Arrow):
            return arrow.get(value)

        if not isinstance(value, return_type):
            raise TypeError(
                f"Provided argument value={value} didn't match signature type={return_type}"
            )

        return value

    def resolve(self, *args, **kwargs):

        mapped_kwargs = {
            self._arg_key_map[key]: self._pythonTypes(self._arg_val_map[key], val)
            for key, val in kwargs.items()
        }

        model: BaseModel | None = self.func(**mapped_kwargs)

        if isinstance(model, list):
            return [WrappedClass(item, self.field_transformer) for item in model]

        elif model is not None:
            return WrappedClass(model, self.field_transformer)
        else:
            return None

    @property
    def gql_field(self) -> GraphQLField:

        args = {}

        sig = inspect.signature(self.func)

        for argname, parameter in sig.parameters.items():
            argname = self.argument_transformer(argname)

            arg_gql_type = self.router.get_gql_type(parameter.annotation, input=True)

            if parameter.default is inspect.Parameter.empty:
                #     arg_gql_type = GraphQLNonNull(arg_gql_type)
                args[argname] = GraphQLArgument(arg_gql_type)
                continue

            args[argname] = GraphQLArgument(
                arg_gql_type, default_value=parameter.default
            )

        if sig.return_annotation is inspect._empty:
            raise GraphQLSchemaUnrecognisedTypeError(
                f"function {self.name} of type {self.type} is missing a return type annotation"
            )

        return GraphQLField(
            type_=self.router.get_gql_type(sig.return_annotation),
            args=args,
            description=self.description,
            deprecation_reason=self.deprecation_warning,
            resolve=self.resolve,
        )


class GraphQLRouter:
    global_excluded_fields: list[str] = ["PK", "SK", "GSI1PK", "GSI1SK"]

    @classmethod
    def combine(cls, routers: list[GraphQLRouter]) -> GraphQLRouter:

        new_router = cls()

        combined_model_directory = {}
        combined_query_directory = {}
        combined_mutation_directory = {}

        for router in routers:
            router.parent = new_router

            for model, registered_model in router._model_directory.items():
                registered_model.router = new_router
                combined_model_directory[model] = registered_model

            for name, registered_function in router._query_directory.items():
                registered_function.router = new_router
                combined_query_directory[name] = registered_function

            for name, registered_function in router._mutation_directory.items():
                registered_function.router = new_router
                combined_mutation_directory[name] = registered_function

        new_router._model_directory = combined_model_directory
        new_router._query_directory = combined_query_directory
        new_router._mutation_directory = combined_mutation_directory

        return new_router

    AWSJSON_SCALAR = GraphQLScalarType("AWSJSON", serialize=lambda x: json.dumps(x))
    JSON_SCALAR = GraphQLScalarType("JSON", serialize=lambda x: json.dumps(x))

    gql_scalar_types_map: dict[type, GraphQLScalarType] = {
        str: GraphQLString,
        int: GraphQLInt,
        float: GraphQLFloat,
        bool: GraphQLBoolean,
        datetime.datetime: GraphQLString,
        arrow.Arrow: GraphQLString,
        dict: AWSJSON_SCALAR,
    }

    def __init__(
        self,
        transformer: BaseTransformer | None = SnakeToCamelCaseTransformer(
            True, True, False, False
        ),
    ):
        self._model_directory: dict[type[BaseModel], RegisteredModel] = {}
        self._query_directory: dict[str, RegisteredFunction] = {}
        self._mutation_directory: dict[str, RegisteredFunction] = {}

        self.parent: GraphQLRouter = None
        self._context = {}
        self._current_event = {}
        self.state = {}

        if transformer is None:
            transformer = IdentityTransformer()

        self.transformer = transformer

        # self.__class__.context = {}
        self.gql_type_mapping: dict[
            type[BaseModel | enum.Enum],
            GraphQLObjectType | GraphQLInputObjectType | GraphQLEnumType,
        ] = {}

    # TODO this behaviour isn't good as you may be getting a reference to the parent, setting a value specifically against that will not update this router but make it available to all other child routers of that parent
    @property
    def context(self) -> dict:
        return self._context or (self.parent and self.parent.context) or self._context

    @context.setter
    def context(self, context: dict):
        self._context = context

    @property
    def current_event(self) -> dict:
        return (
            self._current_event
            or (self.parent and self.parent.current_event)
            or self._context
        )
        # return self._current_event if self.parent is None else (self.parent.current_event or self._current_event)

    @current_event.setter
    def current_event(self, event: dict):
        self._current_event = event

    ################################
    #          Decorators          #
    ################################

    def registerModel(
        self,
        _type: Literal["output", "input"],
        name: str | None = None,
        description: str | None = None,
        exclude_fields: list[str] | None = None,
    ):

        if _type not in ["output", "input"]:
            raise ValueError(
                f"The type [{_type}] is invalid, it must be one of `output`, `input`"
            )

        def register_model(model: type[PydanticModel]) -> type[PydanticModel]:

            model_name = (
                self.transformer.typeTransformer(model.__name__)
                if name is None
                else name
            )

            registered_model = RegisteredModel(
                model=model,
                name=model_name,
                description=description,
                type=_type,
                exclude_fields=(exclude_fields or []) + self.global_excluded_fields,
                field_transformer=self.transformer.fieldTransformer,
                router=self,
            )

            self._model_directory[model] = registered_model

            return model

        return register_model

    def graphQLType(
        self,
        name: str | None = None,
        description: str | None = None,
        exclude_fields: list[str] | None = None,
    ):

        return self.registerModel(
            _type="output",
            name=name,
            description=description,
            exclude_fields=exclude_fields,
        )

    def graphQLInput(
        self,
        name: str | None = None,
        description: str | None = None,
        exclude_fields: list[str] | None = None,
    ):

        return self.registerModel(
            _type="input",
            name=name,
            description=description,
            exclude_fields=exclude_fields,
        )

    def registerFunction(
        self,
        _type: Literal["query", "mutation"],
        name: str | None = None,
        description: str | None = None,
        deprecation_warning: str | None = None,
    ):

        def register_func(func: Callable):

            func_name = (
                self.transformer.funcTransformer(func.__name__)
                if name is None
                else name
            )

            _description = description if description is not None else func.__doc__

            registered_func = RegisteredFunction(
                func=func,
                name=func_name,
                description=_description,
                type=_type,
                router=self,
                transformer=self.transformer,
                argument_transformer=self.transformer.argumentTransformer,
                field_transformer=self.transformer.fieldTransformer,
                deprecation_warning=deprecation_warning,
            )

            if _type == "query":
                self._query_directory[func_name] = registered_func
            elif _type == "mutation":
                self._mutation_directory[func_name] = registered_func
            else:
                raise ValueError(
                    f"The type [{_type}] is invalid, it must be one of `query`, `mutation`"
                )

            return func

        return register_func

    def graphQLQuery(
        self,
        name: str | None = None,
        description: str | None = None,
        deprecation_warning: str | None = None,
    ):

        _wrapper = self.registerFunction(
            "query", name, description, deprecation_warning
        )

        return _wrapper

    def graphQLMutation(
        self,
        name: str | None = None,
        description: str | None = None,
        deprecation_warning: str | None = None,
    ):

        _wrapper = self.registerFunction(
            "mutation", name, description, deprecation_warning
        )

        return _wrapper

    ################################
    #           Resolver           #
    ################################

    def resolve(self, query: str) -> graphql.ExecutionResult:

        # TODO - this call takes 2 ms each time the request is made - could be cached
        schema = self.schema()

        result = graphql.graphql_sync(schema, query)

        return result

    ################################
    #            Schema            #
    ################################

    def get_or_register_model(
        self, model: type[BaseModel], input: bool = False
    ) -> RegisteredModel:

        if model not in self._model_directory:
            self.registerModel("input" if input else "output")(model)

        return self._model_directory[model]

    def get_gql_type(
        self,
        _type: type,
        input: bool = False,
        is_required: bool = True,
        eval_forward_refs: bool = False,
    ) -> GraphQLType:

        output = None

        ## if it's a scalar type, return it
        if _type in self.gql_scalar_types_map:
            output = self.gql_scalar_types_map[_type]

        elif get_origin(_type) in (list, set, frozenset):
            output = GraphQLList(
                self.get_gql_type(
                    _type.__args__[0],
                    input,
                    is_required=True,
                    eval_forward_refs=eval_forward_refs,
                )
            )

        elif get_origin(_type) == typing.Annotated:
            output = self.get_gql_type(
                _type.__args__[0],
                input,
                is_required=is_required,
                eval_forward_refs=eval_forward_refs,
            )
            is_required = False

        elif get_origin(_type) == dict:
            output = self.gql_scalar_types_map[dict]  # Return the json scalar type

        elif get_origin(_type) == Union:
            union_types = _type.__args__

            is_required = type(None) not in union_types

            non_null_types = [
                union_type for union_type in union_types if union_type is not type(None)
            ]

            if len(non_null_types) == 1:
                output = self.get_gql_type(
                    non_null_types[0],
                    input,
                    is_required,
                    eval_forward_refs=eval_forward_refs,
                )

            else:
                raise GraphQLSchemaUnrecognisedTypeError(
                    "Union types are not supported"
                )

        elif get_origin(_type) is not None:
            raise GraphQLSchemaUnrecognisedTypeError(
                f"Unsupported generic type [{_type}]"
            )

        elif isinstance(_type, typing.ForwardRef):
            if eval_forward_refs:
                for pythonClass, output in self.gql_type_mapping.items():
                    if _type.__forward_arg__ == pythonClass.__name__:
                        break

                # output = self.get_gql_type(pythonClass, input, is_required=False, eval_forward_refs=eval_forward_refs)

            else:
                raise ForwardRefException(f"Attempted to evaluate forward ref {_type}")

        elif issubclass(_type, enum.Enum):
            if _type not in self.gql_type_mapping:
                # self.gql_type_mapping[_type] = GraphQLEnumType(_type.__name__, {value.value: value.value for key, value in _type.__members__.items()})
                self.gql_type_mapping[_type] = GraphQLEnumType(
                    _type.__name__, _type, names_as_values=False
                )

            output = self.gql_type_mapping[_type]

        elif issubclass(_type, BaseModel):
            ## if it's a BaseModel, generate the GraphQLObjectType and store in the cache
            if _type not in self.gql_type_mapping:
                registered_model = self.get_or_register_model(_type, input)
                self.gql_type_mapping[_type] = registered_model.gql_type

            output = self.gql_type_mapping[_type]

        else:
            raise GraphQLSchemaUnrecognisedTypeError(f"Unsupported type [{_type}]")

        if is_required:
            return GraphQLNonNull(output)
        else:
            return output

    # def get_gql_queries(self) -> Dict[str, GraphQLField]:

    #     return  {f_name: f.gql_field for f_name, f in self._query_directory.items()}

    # def get_gql_mutations(self) -> Dict[str, GraphQLField]:

    #     return  {f_name: f.gql_field for f_name, f in self._mutation_directory.items()}

    @staticmethod
    def ping() -> str:
        return "pong"

    def schema(self, extending: bool = False):

        _types = [
            self.get_gql_type(model, input=registered_model.type == "input")
            for model, registered_model in tuple(self._model_directory.items())
        ]
        queries = {f_name: f.gql_field for f_name, f in self._query_directory.items()}
        mutations = {
            f_name: f.gql_field for f_name, f in self._mutation_directory.items()
        }

        # Graphql required that a query defined in a valid schema
        if not extending and len(self._query_directory) == 0:
            registered_func = RegisteredFunction(
                func=self.ping,
                name="ping",
                type="query",
                router=self,
                description=None,
                transformer=self.transformer,
            )

            queries["ping"] = registered_func.gql_field

        return GraphQLSchema(
            types=_types,
            query=GraphQLObjectType("Query", queries),
            mutation=GraphQLObjectType("Mutation", mutations) if mutations else None,
        )

    def extension_document_node(self):

        document = graphql.print_schema(self.schema(extending=True))

        if len(self._query_directory):
            document = document.replace("type Query", "extend type Query")
        else:
            document = document.replace("type Query", "")

        if len(self._mutation_directory):
            document = document.replace("type Mutation", "extend type Mutation")
        else:
            document = document.replace("type Mutation", "")

        document_node = graphql.parse(source=graphql.Source(document))

        return document_node
