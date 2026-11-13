"""HTTP error responses must close before control returns to the caller."""

from asyncio import CancelledError
from io import BytesIO
from urllib.error import HTTPError

import pytest

from plane_api.client import PlaneClient, PlaneProviderRequestError


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("body", [b'{"message":"fixture rejection"}', b"not-json", b"\xff"])
def test_request_closes_http_error_after_body_parsing(method, body):
    stream = BytesIO(body)
    error = HTTPError("https://plane.invalid/", 400, "fixture", {}, stream)

    def transport(request, timeout):
        raise error

    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    try:
        with pytest.raises(PlaneProviderRequestError) as caught:
            client._request(method, "/fixture/")
        assert caught.value.http_status == 400
        assert stream.closed
    finally:
        error.close()


@pytest.mark.parametrize("scenario", ["request", "not_found", "governed", "detail_failure"])
def test_http_error_cleanup_failure_preserves_primary_outcome(scenario, monkeypatch):
    class FailingCloseStream(BytesIO):
        def close(self):
            super().close()
            raise OSError("fixture cleanup failure")

    stream = FailingCloseStream(b'{"message":"fixture rejection","token":"fixture-private"}')
    status = 404 if scenario == "not_found" else 400
    error = HTTPError("https://plane.invalid/", status, "fixture", {"Retry-After": "7"}, stream)
    calls = []

    def transport(request, timeout):
        calls.append(request.get_method())
        raise error

    detail_error = RuntimeError("fixture detail failure")

    def fail_detail(exc):
        raise detail_error

    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    if scenario == "not_found":
        assert client._request("GET", "/fixture/", allow_not_found=True) == {"status_code": 404}
    elif scenario == "governed":
        with pytest.raises(RuntimeError, match="HTTP 400") as caught:
            client._governed_request("POST", "/fixture/")
        assert caught.value.__cause__ is error
    elif scenario == "detail_failure":
        monkeypatch.setattr("plane_api.client._sanitize_http_error_detail", fail_detail)
        with pytest.raises(RuntimeError, match="fixture detail failure") as caught:
            client._request("POST", "/fixture/")
        assert caught.value is detail_error
    else:
        with pytest.raises(PlaneProviderRequestError) as caught:
            client._request("POST", "/fixture/", phase="write:issue__add_issue")
        failure = caught.value
        assert failure.__cause__ is error
        assert failure.phase == "write:issue__add_issue"
        assert failure.http_status == 400
        assert failure.transport_class == "http"
        assert failure.deterministic is True
        assert failure.detail == {"message": "fixture rejection", "retry_after_seconds": 7.0}
        assert "fixture-private" not in str(failure.detail)
    assert stream.closed
    assert len(calls) == 1


@pytest.mark.parametrize("governed", [False, True])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, CancelledError])
def test_http_error_cleanup_does_not_suppress_base_exceptions(governed, interrupt):
    class InterruptedCloseStream(BytesIO):
        def close(self):
            super().close()
            raise interrupt()

    stream = InterruptedCloseStream(b"fixture")
    error = HTTPError("https://plane.invalid/", 400, "fixture", {}, stream)
    calls = []

    def transport(request, timeout):
        calls.append(request.get_method())
        raise error

    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    with pytest.raises(interrupt):
        if governed:
            client._governed_request("POST", "/fixture/")
        else:
            client._request("POST", "/fixture/")
    assert stream.closed
    assert len(calls) == 1


def test_request_closes_http_error_on_allowed_not_found():
    stream = BytesIO(b"fixture")
    error = HTTPError("https://plane.invalid/", 404, "fixture", {}, stream)

    def transport(request, timeout):
        raise error

    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    try:
        assert client._request("GET", "/fixture/", allow_not_found=True) == {"status_code": 404}
        assert stream.closed
    finally:
        error.close()


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH"])
def test_governed_request_closes_http_error(method):
    stream = BytesIO(b"fixture")
    error = HTTPError("https://plane.invalid/", 400, "fixture", {}, stream)

    def transport(request, timeout):
        raise error

    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    try:
        with pytest.raises(RuntimeError, match="HTTP 400"):
            client._governed_request(method, "/fixture/")
        assert stream.closed
    finally:
        error.close()


def test_request_closes_http_error_if_detail_processing_raises(monkeypatch):
    stream = BytesIO(b"fixture")
    error = HTTPError("https://plane.invalid/", 400, "fixture", {}, stream)

    def transport(request, timeout):
        raise error

    def fail_detail(exc):
        raise RuntimeError("fixture detail failure")

    monkeypatch.setattr("plane_api.client._sanitize_http_error_detail", fail_detail)
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    try:
        with pytest.raises(RuntimeError, match="fixture detail failure"):
            client._request("GET", "/fixture/")
        assert stream.closed
    finally:
        error.close()
