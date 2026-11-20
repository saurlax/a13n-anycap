"""Bounded HTTPS calls to the AnyCap API."""

import asyncio
import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field, HttpUrl, SecretStr, ValidationError

API_ORIGIN = "https://api.anycap.ai"
MAX_RESPONSE_BYTES = 1 << 20


class AnyCapError(Exception):
    """An AnyCap failure safe to present to an Agent."""


class Artifact(BaseModel):
    type: str
    url: HttpUrl
    mime_type: str


class MediaResult(BaseModel):
    model: str
    outputs: list[Artifact] = Field(min_length=1)
    credit_cost: int | None = None


class TaskStatus(BaseModel):
    task_id: str = Field(pattern=r"^ct_[A-Za-z0-9]{1,64}$")
    status: str
    poll_after_ms: int | None = None
    result: MediaResult | None = None
    error: dict[str, object] | None = None


def error_code(value: object) -> str | None:
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
            return code
    return None


class AnyCapClient:
    def __init__(self, http: httpx.AsyncClient, key: SecretStr) -> None:
        self.http = http
        self.key = key

    async def read(self, path: str, *, params: dict[str, str] | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def generate(self, path: str, body: dict, *, trace_id: str) -> dict:
        return await self._request("POST", path, body=body, trace_id=trace_id)

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict | None = None,
        trace_id: str | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self.key.get_secret_value()}", "Accept-Encoding": "identity"}
        if trace_id is not None:
            headers["X-Trace-ID"] = trace_id
        try:
            async with (
                asyncio.timeout(55),
                self.http.stream(method, API_ORIGIN + path, headers=headers, params=params, json=body) as response,
            ):
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Unexpected content encoding")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response exceeded limit")
                    data.extend(chunk)
                status = response.status_code
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("Unexpected response type")
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            raise self._response_error(trace_id) from None

        if status >= 500 and trace_id is not None:
            raise self._response_error(trace_id)
        if status >= 400:
            code = error_code(payload.get("error"))
            detail = f", {code}" if code else ""
            if status in (401, 403):
                raise AnyCapError(f"AnyCap rejected the workspace credential or capability (HTTP {status}{detail}).")
            raise AnyCapError(f"AnyCap rejected the request (HTTP {status}{detail}).")
        if not 200 <= status < 300 or payload.get("status") != "success":
            raise self._response_error(trace_id)
        return payload

    @staticmethod
    def _response_error(trace_id: str | None) -> AnyCapError:
        if trace_id is not None:
            return AnyCapError(f"AnyCap generation outcome is unknown. Check trace {trace_id} before submitting again.")
        return AnyCapError("AnyCap did not return a usable response.")


def parse_result[ResultT: BaseModel](
    result_type: type[ResultT], payload: dict, *, trace_id: str | None = None
) -> ResultT:
    try:
        return result_type.model_validate(payload.get("data"))
    except ValidationError:
        if trace_id:
            raise AnyCapError(
                f"AnyCap generation outcome is unknown. Check trace {trace_id} before submitting again."
            ) from None
        raise AnyCapError("AnyCap did not return a usable result.") from None
