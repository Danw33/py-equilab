# py-equilab: Equilab API Client Python Library

An **unofficial**, asynchronous, read-only Python client for the Equilab cloud API.

This library provides authentication, token renewal, bounded concurrent reads,
rate-limit observations, Firestore decoding, and typed domain models for Equilab
accounts, horses, training sessions, riders, and stables/groups. It does not modify
cloud data, bypass subscription entitlements, or download route recordings.

> [!IMPORTANT]
> This independent project is unofficial and is not affiliated with, endorsed by,
> or supported by Equilab or Equestrian Insights. The upstream API is undocumented
> and may change without notice. Use only with an account and data you are authorised
> to access.

## Installation

```sh
python -m pip install py-equilab
```

## Usage

```python
import aiohttp

from pyequilab import EquilabClient


async def get_account(email: str, password: str) -> dict:
    async with aiohttp.ClientSession() as session:
        client = EquilabClient(session)
        await client.async_login(email, password)
        return await client.async_get_user()
```

Long-running applications should persist `client.refresh_token` securely. Pass an
`on_token` callback to receive rotated refresh tokens. Callers own the supplied
`aiohttp.ClientSession`; the library never closes it.

## Supported operations

- Sign in and renew authentication tokens.
- Read the authenticated user profile.
- Read referenced horses, training sessions, and stables/groups.
- Read the latest notification without marking it as read.
- Normalise the subset of domain data used by downstream applications.

All public network operations are asynchronous. Exceptions contain safe generic
messages and never include response bodies, credentials, identifiers, or request URLs.

## Development

```sh
python -m pip install -e '.[test]'
pytest --cov=pyequilab --cov-branch --cov-report=term-missing
ruff check .
ruff format --check .
mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the release process.

## Licence

Copyright © 2026 Daniel Wilson ([@Danw33](https://github.com/Danw33))

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Equilab and Equestrian Insights AB are trademarks of their respective owners.
