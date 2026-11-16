## Code styling
- Every new or modified function, class, and module must include concise, and informative documentation.
- There should be inline comments that explains blocks of code logic.
- Inline comments should aim to be a single line long.

## Design principles
- A module should always be able to be read top to bottom with all references already explained before their use. E.g. Whenever possible, place a function definition before the function is called in code.
- Inside a class, private methods should appear before public methods.
- Prefer simple, explicit constraints over complex features. If a feature provides minimal value at the cost of great complexity, discuss alternatives or whether it is valid first and explain the trade-off before implementing full support.
- Considerations/limitations and other "qwerks" of the implementation should be added to a CONSIDERATIONS.md in the docs folder so that it can later be incorporated into the package documentation correctly.