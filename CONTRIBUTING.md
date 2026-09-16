# Contributing

Use Python 3.11 or newer and install the test extra:

```sh
python -m pip install -e '.[test]'
pytest --cov=pyequilab --cov-branch --cov-report=term-missing
ruff check .
ruff format --check .
mypy
```

Changes must remain read-only, avoid logging private data, and include regression
tests. Do not add endpoints that bypass Equilab subscription or access controls.

Releases are tagged from `main`. The GitHub release workflow builds both a wheel and
source distribution, verifies them, and publishes to PyPI using trusted publishing.
