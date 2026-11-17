# Here is an example of the graphql router which would then be connected to appsync

import contextvar

from pynamo.dynamo import DynamoModel, Page, conditions, single_table_indexes
from pynamo.graphql_api import Router

dynamo_table_var = contextvar.ContextVar("dynamo_table")
DynamoModel.default_table(dynamo_table_var.get())


class Post(DynamoModel):
    indexes = single_table_indexes(base=("USER#{user_id}", "POST#{post_id}"))

    user_id: str
    post_id: str
    content: str


class User(DynamoModel):
    indexes = single_table_indexes(base=("USER#{user_id}", "USER"))

    user_id: str
    name: str
    age: int

    @property
    def posts(self):
        return Post.query(
            self._table or dynamo_table_var.get(),
            values={"user_id": self.user_id},
            index_name="user_posts_index",
        )


router = Router()
# router.include_router(post_router)  # Example of including another router


@router.query()
def get_user_info(user_id: str) -> User:
    return User.get(dynamo_table_var.get(), user_id=user_id)


@router.query()
def get_users(
    user_id: str, older_than: int | None, continuation_token: dict | None
) -> Page[User]:
    if older_than is not None:
        return next(
            User.query(
                dynamo_table_var.get(),
                values={"user_id": user_id, "age": conditions.gt(older_than)},
                start_token=continuation_token,
            )
        )
    return next(
        User.query(
            dynamo_table_var.get(),
            values={"user_id": user_id},
            start_token=continuation_token,
        )
    )


@router.mutation()
def update_user(user_id: str, name: str | None, age: int | None) -> User:
    return User.update(
        dynamo_table_var.get(),
        user_id=user_id,
        name=name,
        age=age,
    )
