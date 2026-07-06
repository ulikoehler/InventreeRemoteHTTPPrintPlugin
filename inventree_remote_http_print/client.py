"""HTTP client for the BrotherQL Label Print Service.

Reference: https://github.com/ulikoehler/BrotherQLLabelPrintService

The service is a FastAPI app wrapping ``brother_ql_next``. It exposes a
two-step workflow for printing an image:

    1. ``POST /api/upload``  (multipart)   -> returns ``file_id``
    2. ``POST /api/print``   (JSON body)   -> returns a queue item with ``id``

Printing is asynchronous: the queue item's ``status`` moves from
``queued`` -> ``printing`` -> ``printed`` (or ``failed``). Status is polled
via ``GET /api/queue``.

This module is deliberately framework-agnostic: it has no InvenTree / Django
dependencies, which keeps it easy to unit-test and reuse.
"""

from __future__ import annotations

import io
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


class BrotherQLError(Exception):
    """Raised when the BrotherQL service returns an error or is unreachable."""


class BrotherQLClient:
    """Thin HTTP wrapper around the BrotherQL Label Print Service REST API.

    Parameters
    ----------
    base_url:
        Base URL of the running service, e.g. ``http://localhost:8080``.
        A trailing slash is stripped automatically.
    timeout:
        Per-request HTTP timeout in seconds (used for upload, print, status,
        settings and queue calls).
    verify_ssl:
        Whether to verify TLS certificates. Defaults to ``True``. Set to
        ``False`` only for trusted internal services with self-signed certs.
    session:
        Optional pre-built ``requests.Session`` (mostly for testing / connection
        pooling). If omitted, a new session is created lazily.
    """

    #: Path constants – exposed as class attributes for easy override in tests.
    UPLOAD_PATH = "/api/upload"
    PRINT_PATH = "/api/print"
    QUEUE_PATH = "/api/queue"
    SETTINGS_PATH = "/api/settings"
    PRINTER_STATUS_PATH = "/api/printer/status"
    LABELS_PATH = "/api/labels"
    MODELS_PATH = "/api/models"

    #: Queue item statuses that indicate the job is finished (one way or another).
    TERMINAL_STATUSES = ("printed", "failed")

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        verify_ssl: bool = True,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not base_url:
            raise BrotherQLError("base_url must not be empty")
        # Strip trailing slash so we can safely join with "/api/..."
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self._session = session

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @property
    def session(self) -> requests.Session:
        """Lazily-created ``requests.Session`` (so tests can inject a mock)."""
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _handle_response(self, resp: requests.Response, *, expected: Tuple[int, ...] = (200, 201)) -> Dict[str, Any]:
        """Validate ``resp`` and return its decoded JSON body.

        Raises :class:`BrotherQLError` for non-2xx responses or invalid JSON.
        """
        if resp.status_code not in expected:
            # Try to extract a useful error message from the body.
            message = self._extract_error_message(resp)
            raise BrotherQLError(
                f"BrotherQL service returned HTTP {resp.status_code} for "
                f"{resp.request.method} {resp.request.url}: {message}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise BrotherQLError(
                f"BrotherQL service returned non-JSON response "
                f"(HTTP {resp.status_code}): {resp.text[:200]!r}"
            ) from exc

    @staticmethod
    def _extract_error_message(resp: requests.Response) -> str:
        """Best-effort extraction of an error message from a non-2xx response."""
        try:
            payload = resp.json()
        except ValueError:
            return resp.text[:500]
        # FastAPI HTTPException -> {"detail": "..."}
        if isinstance(payload, dict):
            for key in ("detail", "error", "message"):
                if key in payload:
                    return str(payload[key])
        return str(payload)[:500]

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def upload_file(self, file_bytes: bytes, *, filename: str, content_type: str) -> str:
        """Upload raw file ``bytes`` and return the server-assigned ``file_id``.

        Parameters
        ----------
        file_bytes:
            Raw file content (PNG, PDF, SVG, etc.).
        filename:
            Filename reported to the server — the extension determines how
            the server detects dimensions (e.g. ``.pdf`` → ``pdfinfo``,
            ``.png`` → Pillow with DPI metadata).
        content_type:
            MIME type for the multipart upload (e.g. ``"image/png"``,
            ``"application/pdf"``).
        """
        if not file_bytes:
            raise BrotherQLError("file_bytes must not be empty")
        files = {"file": (filename, file_bytes, content_type)}
        try:
            resp = self.session.post(
                self._url(self.UPLOAD_PATH),
                files=files,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        payload = self._handle_response(resp)
        file_id = payload.get("file_id")
        if not file_id:
            raise BrotherQLError(
                f"BrotherQL upload response did not contain a 'file_id': {payload!r}"
            )
        return str(file_id)

    def upload_png(self, image_bytes: bytes, *, filename: str = "label.png") -> str:
        """Upload raw PNG ``bytes`` and return the server-assigned ``file_id``.

        Convenience wrapper around :meth:`upload_file` with
        ``content_type="image/png"``.
        """
        return self.upload_file(image_bytes, filename=filename, content_type="image/png")

    def print(
        self,
        file_id: str,
        *,
        label: Optional[str] = None,
        orientation: Optional[str] = None,
        resize: Optional[bool] = None,
        copies: int = 1,
    ) -> Dict[str, Any]:
        """Queue a previously-uploaded file for printing.

        Parameters
        ----------
        file_id:
            The ``file_id`` returned by :meth:`upload_png`.
        label:
            Optional ``brother_ql`` label identifier (e.g. ``"62"`` for endless
            62mm tape, or ``"62x29"`` for a die-cut label). Overrides the
            server's configured default.
        orientation:
            ``"portrait"`` or ``"landscape"``. ``None`` lets the server
            auto-detect from the image dimensions (recommended when the PNG
            already matches the tape width).
        resize:
            If ``True``, the server will resize the image to fit the tape
            width. Required if you pass ``orientation`` and the image
            dimensions don't already match the tape width. ``None`` lets the
            server apply its default (no resize).
        copies:
            Number of copies, 1–99 (server-enforced).

        Returns
        -------
        dict
            The full queue item dict returned by ``POST /api/print``. The most
            useful fields are ``id`` (queue item id, for polling) and
            ``status`` (``"queued"`` immediately after submission).
        """
        if not file_id:
            raise BrotherQLError("file_id must not be empty")
        if not isinstance(copies, int) or not (1 <= copies <= 99):
            raise BrotherQLError(f"copies must be an int in [1, 99], got {copies!r}")
        if orientation is not None and orientation not in ("portrait", "landscape"):
            raise BrotherQLError(
                f"orientation must be 'portrait', 'landscape' or None, got {orientation!r}"
            )

        body: Dict[str, Any] = {"file_id": file_id, "copies": copies}
        if label is not None:
            body["label"] = label
        if orientation is not None:
            body["orientation"] = orientation
        if resize is not None:
            body["resize"] = resize

        try:
            resp = self.session.post(
                self._url(self.PRINT_PATH),
                json=body,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        return self._handle_response(resp)

    def list_queue(self) -> List[Dict[str, Any]]:
        """Return the current print queue / history (newest first)."""
        try:
            resp = self.session.get(
                self._url(self.QUEUE_PATH),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        payload = self._handle_response(resp)
        if not isinstance(payload, list):
            raise BrotherQLError(
                f"BrotherQL queue response was not a list: {payload!r}"
            )
        return payload

    def get_queue_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Look up a single queue item by id. Returns ``None`` if not found."""
        for item in self.list_queue():
            if str(item.get("id")) == str(item_id):
                return item
        return None

    def wait_for_completion(
        self,
        item_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Poll ``GET /api/queue`` until ``item_id`` reaches a terminal status.

        Parameters
        ----------
        item_id:
            Queue item id returned by :meth:`print`.
        poll_interval:
            Seconds between polls. Minimum 0.5s to avoid hammering the server.
        timeout:
            Maximum wall-clock seconds to wait before giving up.

        Returns
        -------
        dict
            The final queue item dict. Its ``status`` will be ``"printed"``,
            ``"failed"`` or – if the timeout expired – whatever the last
            observed status was (typically ``"queued"`` or ``"printing"``).

        Raises
        ------
        BrotherQLError
            If the queue item could not be found at all, or if its final
            status is ``"failed"``.
        """
        poll_interval = max(0.5, float(poll_interval))
        deadline = time.monotonic() + max(0.0, float(timeout))

        last_item: Optional[Dict[str, Any]] = None
        while True:
            item = self.get_queue_item(item_id)
            if item is None:
                # The item may have aged out of the history list (capped at
                # ``ui.max_history``, default 100). Treat as a soft success:
                # we queued it successfully and can't confirm otherwise.
                if last_item is not None:
                    return last_item
                raise BrotherQLError(
                    f"Queue item {item_id!r} not found on the BrotherQL service"
                )
            last_item = item
            status = str(item.get("status", "")).lower()
            if status == "printed":
                return item
            if status == "failed":
                err = item.get("error_message") or item.get("page_error") or "unknown print error"
                raise BrotherQLError(f"BrotherQL print job {item_id!r} failed: {err}")
            if time.monotonic() >= deadline:
                # Timed out waiting for terminal status. Return the last seen
                # item rather than raising – the job may still complete later
                # and we don't want to fail the user's print just because the
                # queue is slow.
                return item
            time.sleep(poll_interval)

    def update_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Partial-update the server's persisted config via ``PUT /api/settings``.

        Only the supplied top-level sections (``printer``, ``printing``, ``ui``,
        ``server``, ``storage``) are merged – everything else stays untouched.
        Changes are persisted to ``config.yaml`` on the server.

        Returns the full updated config dict.
        """
        if not settings:
            raise BrotherQLError("settings dict must not be empty")
        try:
            resp = self.session.put(
                self._url(self.SETTINGS_PATH),
                json=settings,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        return self._handle_response(resp)

    def get_settings(self) -> Dict[str, Any]:
        """Return the server's current full config dict."""
        try:
            resp = self.session.get(
                self._url(self.SETTINGS_PATH),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        payload = self._handle_response(resp)
        if not isinstance(payload, dict):
            raise BrotherQLError(
                f"BrotherQL settings response was not a dict: {payload!r}"
            )
        return payload

    def get_printer_status(self) -> str:
        """Return the printer's status string (e.g. ``"ready"``)."""
        try:
            resp = self.session.get(
                self._url(self.PRINTER_STATUS_PATH),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        payload = self._handle_response(resp)
        if "error" in payload:
            raise BrotherQLError(f"Printer status error: {payload['error']}")
        return str(payload.get("status", ""))

    def list_labels(self) -> List[Dict[str, Any]]:
        """Return the list of label types known to ``brother_ql_next``."""
        try:
            resp = self.session.get(
                self._url(self.LABELS_PATH),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        payload = self._handle_response(resp)
        if not isinstance(payload, list):
            raise BrotherQLError(
                f"BrotherQL labels response was not a list: {payload!r}"
            )
        return payload

    def list_models(self) -> List[str]:
        """Return the list of supported printer model identifiers."""
        try:
            resp = self.session.get(
                self._url(self.MODELS_PATH),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BrotherQLError(f"Could not reach BrotherQL service at {self.base_url}: {exc}") from exc
        payload = self._handle_response(resp)
        if not isinstance(payload, list):
            raise BrotherQLError(
                f"BrotherQL models response was not a list: {payload!r}"
            )
        return [str(m) for m in payload]

    # ------------------------------------------------------------------ #
    # High-level convenience
    # ------------------------------------------------------------------ #

    def print_png(
        self,
        image_bytes: bytes,
        *,
        filename: str = "label.png",
        label: Optional[str] = None,
        orientation: Optional[str] = None,
        resize: Optional[bool] = None,
        copies: int = 1,
        wait: bool = False,
        poll_interval: float = 2.0,
        poll_timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """One-shot helper: upload a PNG, queue it for printing, optionally wait.

        See :meth:`upload_png`, :meth:`print` and :meth:`wait_for_completion`
        for parameter docs. When ``wait`` is True, the returned dict is the
        final queue item (after polling); otherwise it's the immediately-
        returned queue item with ``status == "queued"``.
        """
        file_id = self.upload_png(image_bytes, filename=filename)
        item = self.print(
            file_id,
            label=label,
            orientation=orientation,
            resize=resize,
            copies=copies,
        )
        if wait:
            item = self.wait_for_completion(
                item["id"],
                poll_interval=poll_interval,
                timeout=poll_timeout,
            )
        return item

    def print_pil_image(self, image, **kwargs) -> Dict[str, Any]:
        """Convenience wrapper that serialises a PIL Image before calling :meth:`print_png`."""
        return self.print_png(png_bytes_from_pil(image), **kwargs)


# Note: kept as a module-level function (rather than a static method on the
# class) so that test code can safely mock the whole ``BrotherQLClient`` class
# without also losing access to the PNG serialiser. The plugin imports this
# function directly.
def png_bytes_from_pil(image) -> bytes:
    """Serialise a PIL Image to PNG bytes (RGB, no alpha).

    ``brother_ql`` rasterises to 1-bit internally; feeding it an RGBA image
    works, but RGB is unambiguous and matches what ``/api/upload`` expects
    for a "photo" input. Alpha is flattened onto white so transparent pixels
    print as paper (not black).
    """
    if image.mode in ("RGBA", "LA"):
        from PIL import Image  # local import keeps the dep optional at import time
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
