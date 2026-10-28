"""Small authenticated API. Shopee credentials are never an HTTP input/output."""
import hmac
import logging
import math
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .repository import Conflict, Missing, Repository
from .secrets import Cipher

Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$', strict=True)]
ItemId = Annotated[str, Field(pattern=r'^[1-9][0-9]{0,19}$', strict=True)]
RunPath = Annotated[str, Path(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')]
ItemPath = Annotated[str, Path(pattern=r'^[1-9][0-9]{0,19}$')]


class Submit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    runId: Identifier
    productIds: list[ItemId] = Field(min_length=1, max_length=1000)
    deadlineAt: datetime


class Cancel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: Literal['cancelled', 'aborted', 'budget_exhausted', 'deadline_exceeded', 'billing_uncertain', 'backend_unavailable', 'invalid_backend_response'] = 'cancelled'


class Delivery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phase: Literal['delivering', 'delivered', 'charging', 'charged', 'uncertain']
    resultId: str = Field(min_length=1, max_length=256, strict=True)


def create_app(*, repository=None, api_token=None, rate_limit=600):
    @asynccontextmanager
    async def lifespan(app):
        if repository is None:
            settings = Settings.from_env()
            app.state.repository = Repository(settings.database_url, Cipher(settings.secret_key),
                                              min_interval_seconds=settings.min_interval_seconds,
                                              lease_seconds=settings.lease_seconds)
            app.state.api_token = settings.api_token
            app.state.rate_limit = settings.api_rate_limit
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.repository = repository
    app.state.api_token = api_token
    app.state.rate_limit = rate_limit
    recent = deque()

    @app.middleware('http')
    async def guard(request: Request, call_next):
        if request.url.path != '/health':
            authorization = request.headers.get('authorization', '')
            expected = 'Bearer ' + (app.state.api_token or '')
            if not app.state.api_token or not hmac.compare_digest(authorization.encode(), expected.encode()):
                return JSONResponse({'detail': 'Authentication required'}, status_code=401)
            if request.method == 'POST':
                length = request.headers.get('content-length')
                if length is None or not length.isascii() or not length.isdecimal():
                    return JSONResponse({'detail': 'Content-Length required'}, status_code=411)
                if int(length) > 65536:
                    return JSONResponse({'detail': 'Request is too large'}, status_code=413)
            now = time.monotonic()
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= app.state.rate_limit:
                return JSONResponse({'detail': 'Request limit reached'}, status_code=429, headers={'Retry-After': str(min(61, max(1, math.ceil(60 - (now - recent[0])) + 1)))})
            recent.append(now)
        try:
            return await call_next(request)
        except Exception:  # noqa: BLE001 - sanitize third-party failures at HTTP boundary
            logging.getLogger(__name__).error('Backend request failed; details withheld')
            return JSONResponse({'detail': 'Service temporarily unavailable'}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, _error):
        return JSONResponse({'detail': 'Invalid request input'}, status_code=422)

    @app.exception_handler(ValueError)
    async def bad_value(_request, error):
        status = 409 if isinstance(error, Conflict) else 422
        return JSONResponse({'detail': 'Request conflicts with stored state' if status == 409 else 'Invalid request input'}, status_code=status)

    @app.exception_handler(Missing)
    async def missing(_request, _error):
        return JSONResponse({'detail': 'Operation not found'}, status_code=404)

    @app.get('/health')
    def health():
        try:
            ready = app.state.repository is not None and app.state.repository.health()
        except Exception:  # noqa: BLE001 - sanitize third-party failures at HTTP boundary
            ready = False
        if not ready:
            raise HTTPException(status_code=503, detail='Not ready')
        return {'status': 'ok'}

    @app.post('/v1/runs')
    def submit(body: Submit):
        return app.state.repository.submit_run(body.runId, body.productIds, body.deadlineAt)

    @app.get('/v1/runs/{run_id}')
    def get_run(run_id: RunPath, cursor: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        return app.state.repository.get_run(run_id, cursor=cursor, limit=limit)

    @app.post('/v1/runs/{run_id}/cancel')
    def cancel(run_id: RunPath, body: Cancel):
        return app.state.repository.cancel_run(run_id, body.reason)

    @app.get('/v1/runs/{run_id}/deliveries')
    def deliveries(run_id: RunPath):
        return app.state.repository.get_deliveries(run_id)

    @app.post('/v1/runs/{run_id}/deliveries/{item_id}')
    def delivery(run_id: RunPath, item_id: ItemPath, body: Delivery):
        return app.state.repository.record_delivery(run_id, item_id, body.phase, body.resultId)

    return app


app = create_app()
