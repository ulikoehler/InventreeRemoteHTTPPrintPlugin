"""InvenTree label-printing plugin for the BrotherQL Label Print Service.

This plugin sends the rendered label PNG (already rasterised by InvenTree at
300 DPI) to a running BrotherQL Label Print Service instance over HTTP.

The BrotherQL service is a FastAPI app wrapping ``brother_ql_next`` and
talking to a local Brother QL printer via USB or network. Source:
https://github.com/ulikoehler/BrotherQLLabelPrintService

The plugin is configured entirely via InvenTree's standard plugin settings
UI (admin → Settings → Plugin Settings → BrotherQL). The most important
setting is ``SERVER_URL`` – the base URL of the running service.

Print-server-side options that are *not* per-request on the BrotherQL API
(such as ``cut``, ``dither``, ``threshold``, ``hq``) are exposed here as
plugin settings and synchronised to the server via ``PUT /api/settings``
once per print batch, in :meth:`before_printing`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from plugin import InvenTreePlugin
from plugin.mixins import LabelPrintingMixin, SettingsMixin

from . import PLUGIN_VERSION
from .client import BrotherQLClient, BrotherQLError, png_bytes_from_pil

logger = logging.getLogger("inventree.brotherql")


class RemoteHTTPPrintServicePlugin(LabelPrintingMixin, SettingsMixin, InvenTreePlugin):
    """Print InvenTree labels via a remote HTTP print service instance.

    Workflow per printed item:

        1. InvenTree renders the label template to PDF, then to a 300 DPI PNG
           (``png_file`` kwarg, a :class:`PIL.Image.Image`).
        2. This plugin serialises the PNG to bytes and uploads it to the
           BrotherQL service via ``POST /api/upload`` (multipart).
        3. The returned ``file_id`` is queued for printing via
           ``POST /api/print``.
        4. If polling is enabled, the plugin polls ``GET /api/queue`` until
           the queue item reaches ``printed`` or ``failed`` (or the poll
           timeout elapses).

    Configuration lives in :attr:`SETTINGS` (admin-editable). Per-print
    overrides live in :attr:`PrintingOptionsSerializer` (print-dialog fields).
    """

    # ------------------------------------------------------------------ #
    # Plugin metadata (InvenTreePlugin)
    # ------------------------------------------------------------------ #
    NAME = "RemoteHTTPPrintServicePlugin"
    SLUG = "remote-http-print"
    TITLE = _("Remote HTTP print service")
    DESCRIPTION = _(
        "Remote HTTP print service — prints InvenTree labels to a remote "
        "print service over HTTP (https://github.com/ulikoehler/"
        "BrotherQLLabelPrintService). Configure the service base URL in the "
        "plugin settings."
    )
    AUTHOR = "InvenTree community"
    VERSION = PLUGIN_VERSION
    WEBSITE = "https://github.com/ulikoehler/BrotherQLLabelPrintService"
    LICENSE = "MIT"
    PUBLISH_DATE = "2025-07-05"
    MIN_VERSION = "0.16.0"

    # Run print_label inline in the background worker task (one task per batch).
    # This keeps the per-item upload+print+poll sequence simple and lets us
    # surface a useful error to the user via ValidationError on the same task.
    BLOCKING_PRINT = True

    # ------------------------------------------------------------------ #
    # Plugin settings (SettingsMixin)
    # ------------------------------------------------------------------ #
    # The BrotherQL service accepts PNG/JPG/PDF/SVG uploads and rasterises
    # them server-side; print-server-level options (cut, dither, threshold,
    # hq) live in the service's config.yaml and are mutated via
    # PUT /api/settings. We expose them here so InvenTree admins can manage
    # the print service without logging into the BrotherQL host.
    SETTINGS: Dict[str, Dict[str, Any]] = {
        "ENDPOINTS": {
            "name": _("Print endpoints"),
            "description": _(
                "JSON list of endpoints. Each entry is an object with "
                "'name' and 'url' keys, e.g. "
                "[{\"name\": \"Office\", \"url\": \"http://10.0.0.42:8080\"}, "
                "{\"name\": \"Warehouse\", \"url\": \"http://10.0.0.99:8080\"}]."
            ),
            "default": "[]",
            "required": True,
            "validator": str,
        },
        "DEFAULT_ENDPOINT": {
            "name": _("Default endpoint"),
            "description": _(
                "Name of the endpoint to use when none is selected in the "
                "print dialog. Must match a 'name' in the endpoints list. "
                "Leave blank to use the first endpoint."
            ),
            "default": "",
        },
        "REQUEST_TIMEOUT": {
            "name": _("HTTP timeout (s)"),
            "description": _("Per-request HTTP timeout in seconds."),
            "default": 30,
            "validator": [int],
        },
        "VERIFY_SSL": {
            "name": _("Verify SSL certificates"),
            "description": _(
                "Disable only for trusted internal services with self-signed "
                "certificates."
            ),
            "default": True,
            "validator": bool,
        },
        "DEFAULT_LABEL": {
            "name": _("Default label type"),
            "description": _(
                "Optional brother_ql label identifier used when the print "
                "dialog doesn't override it. Examples: '62' (endless 62mm "
                "tape), '62x29' (die-cut), 'd24' (round 24mm). Leave blank "
                "to use the server's configured default."
            ),
            "default": "",
        },
        "DEFAULT_COPIES": {
            "name": _("Default copies"),
            "description": _("Default number of copies when not specified per-print."),
            "default": 1,
            "validator": [int],
        },
        "POLL_STATUS": {
            "name": _("Wait for print completion"),
            "description": _(
                "Poll the BrotherQL queue after submitting each job until it "
                "reaches 'printed' or 'failed'. Disable for fire-and-forget "
                "printing (faster, but errors won't surface in InvenTree)."
            ),
            "default": True,
            "validator": bool,
        },
        "POLL_INTERVAL": {
            "name": _("Poll interval (s)"),
            "description": _("Seconds between queue status polls."),
            "default": 2,
            "validator": [int],
        },
        "POLL_TIMEOUT": {
            "name": _("Poll timeout (s)"),
            "description": _(
                "Maximum seconds to wait for a job to reach 'printed' or "
                "'failed' before returning. The job may still complete later."
            ),
            "default": 60,
            "validator": [int],
        },
        # --- server-side print settings (synced via PUT /api/settings) --- #
        "AUTO_CUT": {
            "name": _("Auto-cut"),
            "description": _("Cut the tape after each label on the printer."),
            "default": True,
            "validator": bool,
        },
        "DITHER": {
            "name": _("Dither"),
            "description": _(
                "Use Floyd–Steinberg dithering when rasterising to 1-bit. "
                "Overrides 'Threshold'."
            ),
            "default": False,
            "validator": bool,
        },
        "THRESHOLD": {
            "name": _("B/W threshold (%)"),
            "description": _(
                "Black/white cutoff percentage (0–100). Ignored when 'Dither' "
                "is enabled."
            ),
            "default": 70,
            "validator": [int],
        },
        "HQ": {
            "name": _("High quality"),
            "description": _("Use the printer's high-quality mode (slower)."),
            "default": True,
            "validator": bool,
        },
        "SYNC_SERVER_SETTINGS": {
            "name": _("Sync print settings to server"),
            "description": _(
                "Push Auto-cut / Dither / Threshold / High quality to the "
                "BrotherQL service at the start of every print batch (via "
                "PUT /api/settings). Disable if you manage the service "
                "config.yaml by hand."
            ),
            "default": True,
            "validator": bool,
        },
    }

    # ------------------------------------------------------------------ #
    # Per-print options (shown in the print dialog)
    # ------------------------------------------------------------------ #
    class PrintingOptionsSerializer(serializers.Serializer):
        """Extra fields rendered in the InvenTree print dialog for this plugin.

        All fields are optional – the plugin falls back to its settings for
        anything the user leaves blank.
        """

        endpoint = serializers.CharField(
            required=False,
            allow_blank=True,
            allow_null=True,
            default=None,
            label=_("Endpoint"),
            help_text=_(
                "Select which print endpoint to use. Leave blank to use "
                "the default endpoint configured in plugin settings."
            ),
        )
        copies = serializers.IntegerField(
            required=False,
            min_value=1,
            max_value=99,
            default=None,
            label=_("Copies"),
            help_text=_("Number of copies to print (1–99)."),
        )
        label = serializers.CharField(
            required=False,
            allow_blank=True,
            allow_null=True,
            default=None,
            label=_("Label type"),
            help_text=_(
                "brother_ql label identifier (e.g. '62', '62x29', 'd24'). "
                "Leave blank to use the plugin default."
            ),
        )
        orientation = serializers.ChoiceField(
            choices=[
                ("auto", _("Auto (server detects)")),
                ("portrait", _("Portrait")),
                ("landscape", _("Landscape")),
            ],
            default="auto",
            label=_("Orientation"),
            help_text=_(
                "Force an orientation. 'Auto' lets the BrotherQL server "
                "detect from the image dimensions (recommended)."
            ),
        )
        resize = serializers.BooleanField(
            required=False,
            default=False,
            label=_("Resize to tape width"),
            help_text=_(
                "If the image dimensions don't match the tape width, "
                "resize it (LANCZOS) before printing. Required when "
                "forcing an orientation that disagrees with the image."
            ),
        )
        wait_for_completion = serializers.BooleanField(
            required=False,
            default=None,
            label=_("Wait for print completion"),
            help_text=_(
                "Override the plugin's 'Wait for print completion' setting "
                "for this print job."
            ),
        )

    # ------------------------------------------------------------------ #
    # Endpoint helpers
    # ------------------------------------------------------------------ #
    def _parse_endpoints(self) -> list[dict[str, str]]:
        """Parse the ENDPOINTS setting into a list of {name, url} dicts."""
        raw = self.get_setting("ENDPOINTS") or "[]"
        try:
            endpoints = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(endpoints, list):
            return []
        result = []
        for ep in endpoints:
            if isinstance(ep, dict) and "name" in ep and "url" in ep:
                result.append({"name": str(ep["name"]), "url": str(ep["url"]).strip()})
        return result

    def _resolve_endpoint(self, override: Optional[str]) -> dict[str, str]:
        """Resolve which endpoint to use.

        Priority: dialog override > DEFAULT_ENDPOINT setting > first endpoint.
        Returns the endpoint dict with 'name' and 'url' keys.
        Raises ValidationError if no endpoints are configured.
        """
        endpoints = self._parse_endpoints()
        if not endpoints:
            raise ValidationError(
                _("No print endpoints configured. Add at least one endpoint "
                  "in Plugin Settings (ENDPOINTS).")
            )

        # Try dialog override
        if override:
            name = str(override).strip()
            for ep in endpoints:
                if ep["name"] == name:
                    return ep
            raise ValidationError(
                _("Endpoint '%(name)s' not found. Available: %(available)s") % {
                    "name": name,
                    "available": ", ".join(ep["name"] for ep in endpoints),
                }
            )

        # Try DEFAULT_ENDPOINT setting
        default_name = (self.get_setting("DEFAULT_ENDPOINT") or "").strip()
        if default_name:
            for ep in endpoints:
                if ep["name"] == default_name:
                    return ep

        # Fall back to first endpoint
        return endpoints[0]

    # ------------------------------------------------------------------ #
    # Client construction
    # ------------------------------------------------------------------ #
    def _get_client(self, endpoint: Optional[dict[str, str]] = None) -> BrotherQLClient:
        """Build a :class:`BrotherQLClient` for the given endpoint.

        If no endpoint is provided, resolves the default endpoint.
        """
        if endpoint is None:
            endpoint = self._resolve_endpoint(None)
        base_url = endpoint["url"]
        if not base_url:
            raise ValidationError(
                _("Endpoint '%(name)s' has no URL configured.") % {"name": endpoint.get("name", "?")}
            )
        try:
            timeout = float(self.get_setting("REQUEST_TIMEOUT"))
        except (TypeError, ValueError):
            timeout = 30.0
        verify_ssl = bool(self.get_setting("VERIFY_SSL"))
        return BrotherQLClient(
            base_url=base_url,
            timeout=timeout,
            verify_ssl=verify_ssl,
        )

    # ------------------------------------------------------------------ #
    # Hooks
    # ------------------------------------------------------------------ #
    def before_printing(self):
        """Called once per print batch before any label is rendered.

        Used to push the server-side print settings (cut, dither, threshold,
        hq) to the print service so the rest of the batch can submit
        plain ``POST /api/print`` requests without re-sending them.
        """
        if not bool(self.get_setting("SYNC_SERVER_SETTINGS")):
            return
        try:
            endpoint = self._resolve_endpoint(None)
            client = self._get_client(endpoint)
        except ValidationError:
            # No endpoints configured yet – let print_label surface the
            # error to the user; we don't want to crash before_printing and
            # mask the real cause.
            logger.warning("No endpoint configured, skipping server-settings sync")
            return
        try:
            settings_payload = {
                "printing": {
                    "cut": bool(self.get_setting("AUTO_CUT")),
                    "dither": bool(self.get_setting("DITHER")),
                    "threshold": int(self.get_setting("THRESHOLD")),
                    "hq": bool(self.get_setting("HQ")),
                },
            }
            client.update_settings(settings_payload)
            logger.info("Synced print settings to endpoint '%s'", endpoint["name"])
        except BrotherQLError as exc:
            # Don't fail the whole batch just because settings sync failed –
            # the printer might still work with whatever config it has.
            logger.warning("Could not sync settings to endpoint '%s': %s", endpoint["name"], exc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Unexpected error syncing settings to endpoint '%s': %s", endpoint["name"], exc)

    # ------------------------------------------------------------------ #
    # The one required method
    # ------------------------------------------------------------------ #
    def print_label(self, **kwargs):
        """Upload and queue one label for printing on the BrotherQL service.

        Receives the standard ``LabelPrintingMixin`` kwargs (see the InvenTree
        docs). We only need a handful:

            pdf_data:         bytes            – raw PDF (unused, we use png_file)
            png_file:         PIL.Image.Image  – rasterised label
            filename:         str              – base filename
            width:            float            – mm (label_instance.width)
            height:           float            – mm (label_instance.height)
            label_instance:   LabelTemplate
            item_instance:    Model
            user:             User | None
            printing_options: dict             – validated PrintingOptionsSerializer data
        """
        png_file = kwargs.get("png_file")
        if png_file is None:
            raise ValidationError(_("Label was not rasterised to a PNG; cannot print."))

        opts: Dict[str, Any] = kwargs.get("printing_options") or {}

        # Resolve endpoint (dialog override > DEFAULT_ENDPOINT > first).
        endpoint = self._resolve_endpoint(opts.get("endpoint"))

        # Resolve each option with fallback to plugin settings.
        copies = self._resolve_copies(opts.get("copies"))
        label = self._resolve_label(opts.get("label"))
        orientation = self._resolve_orientation(opts.get("orientation"))
        resize = self._resolve_resize(opts.get("resize"), orientation)
        wait = self._resolve_wait(opts.get("wait_for_completion"))

        filename = kwargs.get("filename") or "label.png"
        if not filename.lower().endswith(".png"):
            filename = f"{filename}.png"

        try:
            client = self._get_client(endpoint)
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(
                _("Could not build print client for endpoint '%(ep)s': %(err)s") % {
                    "ep": endpoint["name"], "err": exc
                }
            )

        # brother_ql expects the shorter edge to match the tape width. InvenTree
        # rasterises at LABEL_DPI=300 using the template's mm dimensions, so a
        # 62mm-wide template produces a 696px-wide PNG that already matches a
        # 62mm tape. The BrotherQL server will auto-detect this from the upload.
        try:
            image_bytes = png_bytes_from_pil(png_file)
        except Exception as exc:
            raise ValidationError(
                _("Could not serialise label PNG: %(err)s") % {"err": exc}
            )

        logger.info(
            "Printing label '%s' via endpoint '%s' (%dx%d px, %s orientation, %d cop%s)",
            filename,
            endpoint["name"],
            png_file.width,
            png_file.height,
            orientation or "auto",
            copies,
            "y" if copies == 1 else "ies",
        )

        try:
            file_id = client.upload_png(image_bytes, filename=filename)
            queue_item = client.print(
                file_id,
                label=label,
                orientation=orientation,
                resize=resize,
                copies=copies,
            )
        except BrotherQLError as exc:
            raise ValidationError(
                _("BrotherQL print submission failed: %(err)s") % {"err": exc}
            )

        item_id = queue_item.get("id")
        if not wait or not item_id:
            # Fire-and-forget: submission succeeded, that's enough.
            return

        # Poll for completion so failures surface as ValidationErrors.
        try:
            poll_interval = max(1, int(self.get_setting("POLL_INTERVAL")))
            poll_timeout = max(1, int(self.get_setting("POLL_TIMEOUT")))
        except (TypeError, ValueError):
            poll_interval, poll_timeout = 2, 60

        try:
            final = client.wait_for_completion(
                item_id,
                poll_interval=poll_interval,
                timeout=poll_timeout,
            )
        except BrotherQLError as exc:
            raise ValidationError(
                _("BrotherQL print job failed: %(err)s") % {"err": exc}
            )

        # If the poll timed out, log a warning but don't fail the print.
        status = str(final.get("status", "")).lower()
        if status not in BrotherQLClient.TERMINAL_STATUSES:
            logger.warning(
                "BrotherQL: print job %s did not reach a terminal status within %ds "
                "(last status: %s); it may still complete on the server.",
                item_id,
                poll_timeout,
                status,
            )

    # ------------------------------------------------------------------ #
    # Option resolution helpers
    # ------------------------------------------------------------------ #
    def _resolve_copies(self, override: Optional[int]) -> int:
        if override is not None:
            try:
                value = int(override)
            except (TypeError, ValueError):
                value = 1
            return max(1, min(99, value))
        try:
            value = int(self.get_setting("DEFAULT_COPIES"))
        except (TypeError, ValueError):
            value = 1
        return max(1, min(99, value))

    def _resolve_label(self, override: Optional[str]) -> Optional[str]:
        if override:
            value = str(override).strip()
            if value:
                return value
        value = (self.get_setting("DEFAULT_LABEL") or "").strip()
        return value or None

    def _resolve_orientation(self, override: Optional[str]) -> Optional[str]:
        """Translate the dialog's 'auto' to None (don't send the field)."""
        if not override or str(override).lower() == "auto":
            return None
        value = str(override).lower()
        if value not in ("portrait", "landscape"):
            return None
        return value

    def _resolve_resize(self, override: Optional[bool], orientation: Optional[str]) -> Optional[bool]:
        """If the user forced an orientation, default resize to True to avoid 422s."""
        if override is not None:
            return bool(override)
        if orientation is not None:
            return True
        return None

    def _resolve_wait(self, override: Optional[bool]) -> bool:
        if override is not None:
            return bool(override)
        return bool(self.get_setting("POLL_STATUS"))
