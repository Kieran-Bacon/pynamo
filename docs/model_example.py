# Example of the interface - not intended to be a valid python file


class Post(DynamoModel):
    id: str
    user_id: str
    title: str
    content: str

    class Meta:
        indexes = single_table_indexes(base=("USER#{user_id}", "POST#{post_id}"))


class User(DynamoModel):
    id: str
    name: str
    age: int

    @property
    def posts(self) -> Page[Post]:
        return Post.query(values={"user_id": self.id})

    class Meta:
        indexes = single_table_indexes(base=("USER#{user_id}", "USER"))


collection = DynamoCollection(User, Post).query(values={"user_id": "kieran"})


for model in collection[User]:
    ...
