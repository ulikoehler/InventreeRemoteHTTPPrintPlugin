"""Unit tests for :mod:`inventree_remote_http_print.client`.

These cover the HTTP workflow (upload -> print -> poll) against a mocked
BrotherQL service using the `responses` library.
"""

import io
import json

import pytest
import responses
from PIL import Image

from inventree_remote_http_print.client import BrotherQLClient, BrotherQLError


BASE_URL = "http://brotherql.test:8080"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    return BrotherQLClient(base_url=BASE_URL, timeout=5, verify_ssl=True)


@pytest.fixture
def png_bytes():
    img = Image.new("RGB", (696, 303), color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
class TestConstruction:
    def test_strips_trailing_slash(self):
        c = BrotherQLClient("http://example.com/")
        assert c.base_url == "http://example.com"

    def test_rejects_empty_base_url(self):
        with pytest.raises(BrotherQLError):
            BrotherQLClient("")

    def test_lazy_session(self):
        c = BrotherQLClient(BASE_URL)
        assert c._session is None
        s = c.session
        assert s is not None
        # Same instance on second access.
        assert c.session is s


# ---------------------------------------------------------------------------
# upload_png
# ---------------------------------------------------------------------------
class TestUploadPng:
    @responses.activate
    def test_returns_file_id(self, client, png_bytes):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            json={"file_id": "abc-123", "dimensions_px": {"width": 696, "height": 303}},
            status=200,
        )
        file_id = client.upload_png(png_bytes)
        assert file_id == "abc-123"

        # Verify the request body was multipart with the right field name.
        req = responses.calls[0].request
        assert "multipart/form-data" in req.headers.get("Content-Type", "")
        body = req.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        assert 'name="file"' in body
        assert "label.png" in body

    @responses.activate
    def test_missing_file_id_raises(self, client, png_bytes):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            json={"no_file_id": "oops"},
            status=200,
        )
        with pytest.raises(BrotherQLError, match="file_id"):
            client.upload_png(png_bytes)

    @responses.activate
    def test_http_error_raises_with_detail(self, client, png_bytes):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            json={"detail": "Unsupported file type"},
            status=400,
        )
        with pytest.raises(BrotherQLError, match="Unsupported file type"):
            client.upload_png(png_bytes)

    @responses.activate
    def test_non_json_response_with_5xx(self, client, png_bytes):
        # A 5xx with an HTML body: the client should surface the HTML excerpt
        # as the error message (more useful than "non-JSON" alone).
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            body="<html>500 server error</html>",
            status=500,
        )
        with pytest.raises(BrotherQLError, match="500 server error"):
            client.upload_png(png_bytes)

    @responses.activate
    def test_non_json_response_with_2xx(self, client, png_bytes):
        # A 2xx response with a non-JSON body is a protocol violation: the
        # client should raise the "non-JSON" error path.
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            body="not json at all",
            status=200,
            content_type="text/plain",
        )
        with pytest.raises(BrotherQLError, match="non-JSON"):
            client.upload_png(png_bytes)

    def test_connection_error_wrapped(self, png_bytes):
        # Simulate a network failure by injecting a session mock that raises.
        # (Doing this through `responses`'s `body=Exception` API is unreliable
        # across versions, so we patch the session directly.)
        from unittest import mock
        import requests as _requests
        session = mock.Mock()
        session.post.side_effect = _requests.ConnectionError("connection refused")
        client = BrotherQLClient(base_url=BASE_URL, session=session)
        with pytest.raises(BrotherQLError, match="Could not reach"):
            client.upload_png(png_bytes)

    def test_empty_bytes_raises(self, client):
        with pytest.raises(BrotherQLError, match="empty"):
            client.upload_png(b"")


# ---------------------------------------------------------------------------
# print
# ---------------------------------------------------------------------------
class TestPrint:
    @responses.activate
    def test_minimal_request_body(self, client):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/print",
            json={"id": "print-1", "status": "queued"},
            status=200,
        )
        item = client.print("abc-123")
        assert item["id"] == "print-1"
        body = json.loads(responses.calls[0].request.body)
        assert body == {"file_id": "abc-123", "copies": 1}

    @responses.activate
    def test_full_request_body(self, client):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/print",
            json={"id": "print-1", "status": "queued"},
            status=200,
        )
        client.print(
            "abc-123",
            label="62",
            orientation="landscape",
            resize=True,
            copies=3,
        )
        body = json.loads(responses.calls[0].request.body)
        assert body == {
            "file_id": "abc-123",
            "copies": 3,
            "label": "62",
            "orientation": "landscape",
            "resize": True,
        }

    def test_invalid_copies_raises(self, client):
        with pytest.raises(BrotherQLError, match="copies"):
            client.print("abc-123", copies=0)
        with pytest.raises(BrotherQLError, match="copies"):
            client.print("abc-123", copies=100)
        with pytest.raises(BrotherQLError, match="copies"):
            client.print("abc-123", copies="three")

    def test_invalid_orientation_raises(self, client):
        with pytest.raises(BrotherQLError, match="orientation"):
            client.print("abc-123", orientation="sideways")

    def test_empty_file_id_raises(self, client):
        with pytest.raises(BrotherQLError, match="file_id"):
            client.print("")

    @responses.activate
    def test_http_422_raises(self, client):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/print",
            json={"detail": "Image dimensions do not match tape width and resize was not requested"},
            status=422,
        )
        with pytest.raises(BrotherQLError, match="dimensions do not match"):
            client.print("abc-123", orientation="portrait")


# ---------------------------------------------------------------------------
# Queue polling
# ---------------------------------------------------------------------------
class TestQueuePolling:
    @responses.activate
    def test_list_queue(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[
                {"id": "print-2", "status": "printed"},
                {"id": "print-1", "status": "queued"},
            ],
            status=200,
        )
        items = client.list_queue()
        assert len(items) == 2
        assert client.get_queue_item("print-1")["status"] == "queued"
        assert client.get_queue_item("print-2")["status"] == "printed"
        assert client.get_queue_item("print-99") is None

    @responses.activate
    def test_wait_for_completion_returns_on_printed(self, client):
        # First poll: still queued. Second poll: printed.
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[{"id": "p1", "status": "queued"}],
            status=200,
        )
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[{"id": "p1", "status": "printed"}],
            status=200,
        )
        result = client.wait_for_completion("p1", poll_interval=0.01, timeout=5)
        assert result["status"] == "printed"
        assert len(responses.calls) == 2

    @responses.activate
    def test_wait_for_completion_raises_on_failed(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[{"id": "p1", "status": "failed", "error_message": "no paper"}],
            status=200,
        )
        with pytest.raises(BrotherQLError, match="no paper"):
            client.wait_for_completion("p1", poll_interval=0.01, timeout=5)

    @responses.activate
    def test_wait_for_completion_returns_on_timeout(self, client):
        # Always queued, never finishes.
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[{"id": "p1", "status": "queued"}],
            status=200,
        )
        result = client.wait_for_completion("p1", poll_interval=0.01, timeout=0.05)
        assert result["status"] == "queued"

    @responses.activate
    def test_wait_for_completion_raises_when_item_missing(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[{"id": "other", "status": "printed"}],
            status=200,
        )
        with pytest.raises(BrotherQLError, match="not found"):
            client.wait_for_completion("p1", poll_interval=0.01, timeout=0.5)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class TestSettings:
    @responses.activate
    def test_update_settings(self, client):
        responses.add(
            responses.PUT,
            f"{BASE_URL}/api/settings",
            json={"printing": {"cut": False, "dither": True, "threshold": 70, "hq": True}},
            status=200,
        )
        result = client.update_settings({"printing": {"cut": False, "dither": True}})
        assert result["printing"]["cut"] is False
        body = json.loads(responses.calls[0].request.body)
        assert body == {"printing": {"cut": False, "dither": True}}

    def test_empty_settings_raises(self, client):
        with pytest.raises(BrotherQLError, match="empty"):
            client.update_settings({})

    @responses.activate
    def test_get_settings(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/settings",
            json={"printer": {"model": "QL-820NWB"}, "printing": {"cut": True}},
            status=200,
        )
        result = client.get_settings()
        assert result["printer"]["model"] == "QL-820NWB"


# ---------------------------------------------------------------------------
# Status / labels / models
# ---------------------------------------------------------------------------
class TestMisc:
    @responses.activate
    def test_get_printer_status(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/printer/status",
            json={"status": "ready"},
            status=200,
        )
        assert client.get_printer_status() == "ready"

    @responses.activate
    def test_get_printer_status_error(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/printer/status",
            json={"error": "printer offline"},
            status=200,
        )
        with pytest.raises(BrotherQLError, match="printer offline"):
            client.get_printer_status()

    @responses.activate
    def test_list_labels(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/labels",
            json=[{"identifier": "62", "name": "62mm endless"}, {"identifier": "d24", "name": "round 24mm"}],
            status=200,
        )
        labels = client.list_labels()
        assert len(labels) == 2
        assert labels[0]["identifier"] == "62"

    @responses.activate
    def test_list_models(self, client):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/models",
            json=["QL-800", "QL-820NWB"],
            status=200,
        )
        assert client.list_models() == ["QL-800", "QL-820NWB"]


# ---------------------------------------------------------------------------
# High-level convenience
# ---------------------------------------------------------------------------
class TestPrintPng:
    @responses.activate
    def test_print_png_no_wait(self, client, png_bytes):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            json={"file_id": "f1"},
            status=200,
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/print",
            json={"id": "p1", "status": "queued"},
            status=200,
        )
        item = client.print_png(png_bytes, copies=2, label="62")
        assert item["status"] == "queued"
        # Verify the print body.
        body = json.loads(responses.calls[1].request.body)
        assert body == {"file_id": "f1", "copies": 2, "label": "62"}

    @responses.activate
    def test_print_png_with_wait(self, client, png_bytes):
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            json={"file_id": "f1"},
            status=200,
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/print",
            json={"id": "p1", "status": "queued"},
            status=200,
        )
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[{"id": "p1", "status": "printing"}],
            status=200,
        )
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/queue",
            json=[{"id": "p1", "status": "printed"}],
            status=200,
        )
        item = client.print_png(png_bytes, wait=True, poll_interval=0.01, poll_timeout=5)
        assert item["status"] == "printed"

    @responses.activate
    def test_print_pil_image_flattens_alpha(self, client):
        # RGBA image with a transparent corner – should flatten to white RGB.
        img = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/upload",
            json={"file_id": "f1"},
            status=200,
        )
        responses.add(
            responses.POST,
            f"{BASE_URL}/api/print",
            json={"id": "p1", "status": "queued"},
            status=200,
        )
        client.print_pil_image(img)
        # The uploaded PNG body should decode as PNG and be RGB.
        upload_body = responses.calls[0].request.body
        # Multipart bodies are bytes; pull the PNG chunk out by re-decoding.
        if isinstance(upload_body, (bytes, bytearray)):
            # Find the PNG signature inside the multipart payload.
            png_sig = b"\x89PNG\r\n\x1a\n"
            idx = upload_body.find(png_sig)
            assert idx != -1, "PNG signature not found in upload body"
            # The IEND chunk marker closes the PNG data.
            iend = upload_body.find(b"IEND", idx)
            assert iend != -1, "IEND marker not found"
            png_bytes = upload_body[idx:iend + 8]  # IEND + 4-byte CRC
            decoded = Image.open(io.BytesIO(png_bytes))
            assert decoded.mode == "RGB"

    def test_module_level_png_bytes_from_pil_flattens_alpha(self):
        # Direct unit test for the module-level helper.
        from inventree_remote_http_print.client import png_bytes_from_pil
        img = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
        b = png_bytes_from_pil(img)
        decoded = Image.open(io.BytesIO(b))
        assert decoded.mode == "RGB"
        # Top-left pixel (which was fully transparent) should now be white.
        assert decoded.getpixel((0, 0)) == (255, 255, 255)
