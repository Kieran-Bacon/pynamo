# Next steps: redesigning the `DynamoModel` interface

Notes from 2026-11-17, to pick up the next session. Nothing below has been implemented yet.

## Where things stand

- Branch `feature/graphql`, working tree clean apart from this file. Nothing has been pushed.
- Recent commits:
  - `7a01b77` removed the GraphQL `ID` type, so models don't depend on `pynamo.api`. Identifiers are published as `String`, as noted in `docs/CONSIDERATIONS.md`.
  - `e7f6b76` measures test coverage on every pytest run.
  - `46b5ced` renamed the package to `tidal`.
  - `11c2cf8` switched the tests to pytest, with one test file per module.
- 116 tests pass, and ruff and ty are clean.
- Commands:
  - tests: `uv run --extra graphql python -m pytest`
  - lint and format: `uvx ruff check` and `uvx ruff format`
  - type check: `uvx ty check`

## The three problems with the model interface

### 1. `indexes` on the class body reserves a field name

A model can't have a field called `indexes`, because `DynamoModel` reads it as a `ClassVar` (`src/pynamo/models/base.py`, in `__pydantic_init_subclass__`). The same applies to the method names: `get`, `put`, `query`, `scan`, `update`, `delete`, `remove`, `attach`, `checkpoint`. Pydantic has the same problem with `model_*`, and nothing can fully avoid it.

The plan is to move the model's options into a single container. **This is the first decision to make**, as everything else builds on it.

- **Nested `class Meta:`**, as Django and PynamoDB do:
  - Familiar, and pydantic ignores nested classes.
  - Type checkers don't check what's inside it.
  - A subclass's `Meta` replaces its parent's unless it subclasses `Parent.Meta`, which is a well-known Django gotcha.
- **A typed `dynamo_config = DynamoConfig(...)`**, mirroring pydantic's `model_config` (Claude's leaning):
  - Type-checked, and IDEs autocomplete it.
  - A subclass's options can be merged over its parent's, the way `model_config` is. A base model then sets the table once, and each entity adds only its indexes.
  - It reserves the name `dynamo_config`, which no one should need as a field.

### 2. Keys that are plain columns (non-single-table designs)

Today every key is a separate attribute, generated from a `str.format` template (`src/pynamo/models/indexes.py`). With a template like `"{user_id}"`, the key is converted to a string and written alongside the field. That breaks number keys, which DynamoDB stores as `N`, and turns comparisons on the sort key, such as `gt` on a numeric `created_at`, into string comparisons.

Proposal: each key is one of two kinds.

- **A field name**, e.g. `pk="customer_id"`. The key *is* that column:
  - nothing extra is written, and the value keeps its type;
  - key conditions use the value as it is;
  - updating a secondary key field is an ordinary update, with no key to regenerate;
  - at class creation, check that the field exists and has a type DynamoDB allows for keys (`str`, `int`, `Decimal`, `bytes`).
- **`Template("PK", "USER#{user_id}")`**: a generated attribute, as every key is today.

Each key needs its kind marked explicitly, because a bare string is ambiguous. `"ORDER"` is a constant sort key in the tests, but it could equally be a field name. `single_table_indexes` keeps its tuple shorthand and builds `Template` keys, so single-table users don't notice.

The difference in behaviour, to add to `docs/CONSIDERATIONS.md`: `scan` filters to a model's items by key prefix (e.g. `CUSTOMER#`), and that only works for template keys. A model keyed by columns matches every item in its table, which is correct when it's the only model in that table.

Sketch:

```python
class Order(DynamoModel):
    dynamo_config = DynamoConfig(
        table_name="orders",
        indexes=[
            PrimaryIndex(pk="customer_id", sk="order_id"),
            GlobalSecondaryIndex("by_status", pk="status", sk="created_at"),
        ],
    )
```

Open questions:

- Should a template key and a column key be mixable in one index? For example, a column partition key with a template sort key. Probably yes, as each key is handled on its own.
- What should `PrimaryIndex` and the other index classes look like now: `pk`/`sk` accepting `str | Template`, instead of `pk_name` and `pk_template`?
- How should column-key values be serialized for `get`/`remove`? For example, an `Enum` or `datetime` field used as a key. The simple option is to only allow `str`, `int`, `Decimal` and `bytes`.

### 3. Table name and session in the options

Today the table is a boto3 `Table` resource, set with `DynamoModel.default_table(table)` or passed per call.

Proposal:

- Add `table_name: str` and `session: boto3.Session | None` to the options, falling back to the default boto3 session.
- Create the resource on first use and cache it. Importing a model should then need no AWS credentials or region; at the moment the docs example and import checks need dummy credentials.
- Keep `default_table(table)` so tests and mocks can supply a resource.
- The order of precedence:
  1. the per-call `table=`;
  2. then `default_table`;
  3. then `table_name` with `session`.

Open question: should `default_table` stay, or does setting `table_name` on a base model replace it? It's needed for tests either way, unless the tests pass `table=` everywhere.

## Suggested order for tomorrow

1. Decide between `Meta` and `dynamo_config` (problem 1).
2. Settle the key and index API (problem 2): the `Template` name, the index class fields, and the allowed column-key types.
3. Implement, roughly in this order:
   - `indexes.py`: new key types and index classes, and `single_table_indexes` on top of them.
   - `base.py`: reading the options in `__pydantic_init_subclass__`, then `_keys`, `_primary_key`, `_updated_keys`, `query`/`scan` and the prefix filters.
   - `conditions.py`: key conditions for column keys, using typed values and comparators directly.
   - `updates.py`: check that updates to column keys don't regenerate anything.
   - The table: `table_name`, `session`, the lazy resource, and the precedence rules.
4. Update the tests in `tests/test_models/` (the `order_model` fixture in `conftest.py` defines the shared model), `docs/README.md`, `docs/new_graphql_router.py` and the models docs, plus `docs/CONSIDERATIONS.md`.

## Smaller open items

- **`EventName` in `src/pynamo/streams/events.py`:** keep it as a `Literal`, so users can pass plain strings without importing anything. But build `ALL_EVENTS` from it with `typing.get_args(EventName.__value__)` instead of repeating the values, and have `stream.handle` reject unknown event names. Not yet confirmed.
- **Page tokens in the API:** keep `AWSJSON`, or switch to an opaque string token? Undecided.
- **The users and posts example (`docs/new_graphql_router.py`):** `Post.query({"user_id": ...})` with no sort key condition also reads the `User` item in that partition, wasting a read. It needs a sort key prefix condition, which the column and template key work may affect.
