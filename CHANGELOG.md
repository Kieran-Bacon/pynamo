# Changelog

All notable changes to this project are documented in this file.

## [0.1.0b2] - Unreleased

### Changed

- `get`, `remove` and `update` no longer need a key for models whose key templates reference no attributes, such as single-item settings models: `Settings.get()`, `Settings.update(changes={...})` and `Settings.remove()`. Leaving the key out on a model whose key needs attribute values still raises `ValueError`.

## [0.1.0b1]

- First beta release.
