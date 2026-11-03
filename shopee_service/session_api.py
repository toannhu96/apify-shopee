"""Bounded operator-only session handoff; no credential-bearing responses."""
import json
from functools import partial
from typing import Annotated, Literal

from fastapi import HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from shopee_affiliate import signed_client

from . import cli, worker
from .repository import Missing

MAX_SESSION_BYTES = 2 * signed_client.MAX_COOKIE_INPUT_BYTES
AccountPath = Annotated[str, Path(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
STATUSES = frozenset({'healthy', 'pending', 'authentication', 'verification', 'signing',
                      'rate_limit', 'disabled', 'unavailable'})


class SessionUpload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_version: int = Field(ge=1, le=9999999999)
    item_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    cookie_source: str
    headers: dict[str, str]
    signing_mode: Literal['cumulative', 'standalone-seed'] = 'cumulative'


def account_status(repo, alias):
    row = next((row for row in repo.list_accounts() if row['alias'] == alias), None)
    if row is None:
        raise Missing('Account not found')
    return {'alias': alias, 'version': row['version'],
            'status': row['status'] if row['status'] in STATUSES else 'unavailable'}


def replace_session(repo, alias, raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=signed_client._pairs,
                           parse_constant=signed_client._constant,
                           parse_float=signed_client._finite_float)
        body = SessionUpload.model_validate(value)
        credentials = cli.session_credentials(json.dumps({
            'cookie_source': body.cookie_source, 'headers': body.headers,
        }).encode(), body.signing_mode)
    except Exception:  # noqa: BLE001 - validation errors can retain cookies and signing headers
        raise ValueError('Invalid session handoff') from None
    version = repo.replace_account(alias, credentials, require_existing=True,
                                   expected_version=body.expected_version,
                                   preserve_proxy=True, require_enabled=True)
    return cli.verification_result(repo, alias, body.item_id, version,
                                   verify=partial(worker.verify_account, expected_version=version))


def register_session_routes(app):
    @app.get('/v1/admin/accounts/{alias}')
    def account(alias: AccountPath):
        return account_status(app.state.repository, alias)

    @app.post('/v1/admin/accounts/{alias}/session')
    async def refresh(alias: AccountPath, request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_SESSION_BYTES:
                raise HTTPException(status_code=413, detail='Request is too large')
            raw.extend(chunk)
        if len(raw) != int(request.headers['content-length']):
            raise HTTPException(status_code=422, detail='Invalid request input')
        return await run_in_threadpool(replace_session, app.state.repository, alias, bytes(raw))
