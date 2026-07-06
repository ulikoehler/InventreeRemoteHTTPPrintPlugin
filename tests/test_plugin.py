"""Unit tests for :mod:`inventree_remote_http_print.plugin`.

These verify:
  * the plugin's METADATA / SETTINGS shape,
  * per-print option resolution (dialog overrides vs. plugin settings),
  * the full print_label workflow (upload -> print -> poll) with the client
    mocked out,
  * error propagation as ``ValidationError``.

Django / DRF / InvenTree are stubbed in ``conftest.py`` so we don't need a
full InvenTree install.
"""

import io
import json
from unittest import mock

import pytest
from PIL import Image

from inventree_remote_http_print.client import BrotherQLError
from inventree_remote_http_print.plugin import RemoteHTTPPrintServicePlugin
from inventree_remote_http_print import PLUGIN_VERSION


def _set_endpoints(plugin, endpoints):
    """Helper to set the ENDPOINTS setting from a list of dicts."""
    plugin.set_setting_for_test("ENDPOINTS", json.dumps(endpoints))


# ---------------------------------------------------------------------------
# Metadata / shape
# ---------------------------------------------------------------------------
class TestPluginMetadata:
    def test_basic_metadata(self, plugin_instance):
        assert plugin_instance.SLUG == "remote-http-print"
        assert plugin_instance.NAME == "Remote HTTP print"
        assert plugin_instance.VERSION == PLUGIN_VERSION
        assert plugin_instance.MIN_VERSION == "0.16.0"
        assert plugin_instance.BLOCKING_PRINT is True

    def test_admin_source_set(self, plugin_instance):
        assert plugin_instance.ADMIN_SOURCE == "endpoint_editor.js"

    def test_get_admin_context(self, plugin_instance):
        ctx = plugin_instance.get_admin_context()
        assert ctx["slug"] == "remote-http-print"
        assert "settings" in ctx

    def test_required_settings_present(self, plugin_instance):
        # ENDPOINTS is the one absolutely-required setting.
        assert "ENDPOINTS" in plugin_instance.SETTINGS
        assert plugin_instance.SETTINGS["ENDPOINTS"].get("required") is True

    def test_settings_have_names_and_descriptions(self, plugin_instance):
        for key, entry in plugin_instance.SETTINGS.items():
            assert "name" in entry, f"setting {key} missing 'name'"
            assert "description" in entry, f"setting {key} missing 'description'"

    def test_printing_options_serializer_has_expected_fields(self, plugin_instance):
        serializer = plugin_instance.PrintingOptionsSerializer()
        # The stub Serializer doesn't expose declared fields by name; check
        # the class attributes instead.
        declared = {
            name for name in dir(plugin_instance.PrintingOptionsSerializer)
            if not name.startswith("_") and name not in ("data", "initial_data", "errors", "is_valid")
        }
        for expected in ("endpoint", "copies", "label", "orientation", "resize", "wait_for_completion"):
            assert expected in declared, f"missing printing option field: {expected}"


# ---------------------------------------------------------------------------
# Option resolution
# ---------------------------------------------------------------------------
class TestOptionResolution:
    def test_copies_dialog_override(self, plugin_instance):
        assert plugin_instance._resolve_copies(5) == 5

    def test_copies_clamps_to_range(self, plugin_instance):
        assert plugin_instance._resolve_copies(0) == 1
        assert plugin_instance._resolve_copies(1000) == 99

    def test_copies_falls_back_to_setting(self, plugin_instance):
        plugin_instance.set_setting_for_test("DEFAULT_COPIES", 3)
        assert plugin_instance._resolve_copies(None) == 3

    def test_copies_invalid_falls_back_to_1(self, plugin_instance):
        plugin_instance.set_setting_for_test("DEFAULT_COPIES", "garbage")
        assert plugin_instance._resolve_copies(None) == 1

    def test_label_dialog_override(self, plugin_instance):
        assert plugin_instance._resolve_label("62x29") == "62x29"

    def test_label_blank_falls_back_to_setting(self, plugin_instance):
        plugin_instance.set_setting_for_test("DEFAULT_LABEL", "62")
        assert plugin_instance._resolve_label(None) == "62"
        assert plugin_instance._resolve_label("") == "62"

    def test_label_no_setting_no_default(self, plugin_instance):
        plugin_instance.set_setting_for_test("DEFAULT_LABEL", "")
        assert plugin_instance._resolve_label(None) is None

    def test_orientation_auto_returns_none(self, plugin_instance):
        assert plugin_instance._resolve_orientation("auto") is None
        assert plugin_instance._resolve_orientation(None) is None

    def test_orientation_explicit(self, plugin_instance):
        assert plugin_instance._resolve_orientation("portrait") == "portrait"
        assert plugin_instance._resolve_orientation("landscape") == "landscape"

    def test_orientation_invalid_returns_none(self, plugin_instance):
        assert plugin_instance._resolve_orientation("sideways") is None

    def test_resize_explicit_override(self, plugin_instance):
        assert plugin_instance._resolve_resize(True, None) is True
        assert plugin_instance._resolve_resize(False, "portrait") is False

    def test_resize_defaults_true_when_orientation_forced(self, plugin_instance):
        # Avoids the BrotherQL 422 "dimensions don't match tape" rejection.
        assert plugin_instance._resolve_resize(None, "portrait") is True
        assert plugin_instance._resolve_resize(None, "landscape") is True

    def test_resize_defaults_none_when_auto(self, plugin_instance):
        assert plugin_instance._resolve_resize(None, None) is None

    def test_wait_dialog_override(self, plugin_instance):
        assert plugin_instance._resolve_wait(True) is True
        assert plugin_instance._resolve_wait(False) is False

    def test_wait_falls_back_to_setting(self, plugin_instance):
        plugin_instance.set_setting_for_test("POLL_STATUS", True)
        assert plugin_instance._resolve_wait(None) is True
        plugin_instance.set_setting_for_test("POLL_STATUS", False)
        assert plugin_instance._resolve_wait(None) is False


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------
class TestEndpointResolution:
    def test_no_endpoints_raises(self, plugin_instance):
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError, match="No print endpoints"):
            plugin_instance._resolve_endpoint(None)

    def test_first_endpoint_used_by_default(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer1.local:8080"},
            {"name": "Warehouse", "url": "http://printer2.local:8080"},
        ])
        ep = plugin_instance._resolve_endpoint(None)
        assert ep["name"] == "Office"
        assert ep["url"] == "http://printer1.local:8080"

    def test_default_endpoint_setting_used(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer1.local:8080"},
            {"name": "Warehouse", "url": "http://printer2.local:8080"},
        ])
        plugin_instance.set_setting_for_test("DEFAULT_ENDPOINT", "Warehouse")
        ep = plugin_instance._resolve_endpoint(None)
        assert ep["name"] == "Warehouse"

    def test_dialog_override_takes_priority(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer1.local:8080"},
            {"name": "Warehouse", "url": "http://printer2.local:8080"},
        ])
        plugin_instance.set_setting_for_test("DEFAULT_ENDPOINT", "Office")
        ep = plugin_instance._resolve_endpoint("Warehouse")
        assert ep["name"] == "Warehouse"

    def test_invalid_endpoint_name_raises(self, plugin_instance):
        from django.core.exceptions import ValidationError
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer1.local:8080"},
        ])
        with pytest.raises(ValidationError, match="not found"):
            plugin_instance._resolve_endpoint("Nonexistent")

    def test_parse_endpoints_handles_invalid_json(self, plugin_instance):
        plugin_instance.set_setting_for_test("ENDPOINTS", "not json")
        assert plugin_instance._parse_endpoints() == []

    def test_parse_endpoints_filters_missing_keys(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "OK", "url": "http://ok:8080"},
            {"name": "NoUrl"},
            {"url": "http://noname:8080"},
        ])
        eps = plugin_instance._parse_endpoints()
        assert len(eps) == 1
        assert eps[0]["name"] == "OK"


class TestClientConstruction:
    def test_no_endpoints_raises_validation_error(self, plugin_instance):
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError, match="No print endpoints"):
            plugin_instance._get_client()

    def test_client_built_from_first_endpoint(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080/"},
        ])
        plugin_instance.set_setting_for_test("REQUEST_TIMEOUT", "15")
        plugin_instance.set_setting_for_test("VERIFY_SSL", False)
        client = plugin_instance._get_client()
        assert client.base_url == "http://printer.local:8080"  # trailing slash stripped
        assert client.timeout == 15.0
        assert client.verify_ssl is False

    def test_client_built_from_named_endpoint(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer1.local:8080"},
            {"name": "Warehouse", "url": "http://printer2.local:8080"},
        ])
        ep = plugin_instance._resolve_endpoint("Warehouse")
        client = plugin_instance._get_client(ep)
        assert client.base_url == "http://printer2.local:8080"

    def test_invalid_timeout_falls_back_to_default(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("REQUEST_TIMEOUT", "garbage")
        client = plugin_instance._get_client()
        assert client.timeout == 30.0


# ---------------------------------------------------------------------------
# before_printing
# ---------------------------------------------------------------------------
class TestBeforePrinting:
    def test_no_sync_when_disabled(self, plugin_instance):
        plugin_instance.set_setting_for_test("SYNC_SERVER_SETTINGS", False)
        with mock.patch.object(plugin_instance, "_get_client") as m:
            plugin_instance.before_printing()
            m.assert_not_called()

    def test_no_crash_when_no_endpoints(self, plugin_instance):
        # No endpoints configured -> _resolve_endpoint raises ValidationError.
        # before_printing should swallow this and continue (so print_label
        # surfaces the real error later).
        plugin_instance.set_setting_for_test("SYNC_SERVER_SETTINGS", True)
        # Should not raise.
        plugin_instance.before_printing()

    def test_sync_pushes_settings_to_server(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("SYNC_SERVER_SETTINGS", True)
        plugin_instance.set_setting_for_test("AUTO_CUT", False)
        plugin_instance.set_setting_for_test("DITHER", True)
        plugin_instance.set_setting_for_test("THRESHOLD", 80)
        plugin_instance.set_setting_for_test("HQ", False)

        with mock.patch("inventree_remote_http_print.plugin.BrotherQLClient") as ClientCls:
            mock_client = ClientCls.return_value
            plugin_instance.before_printing()
            mock_client.update_settings.assert_called_once()
            sent = mock_client.update_settings.call_args[0][0]
            assert sent == {
                "printing": {"cut": False, "dither": True, "threshold": 80, "hq": False}
            }

    def test_sync_error_does_not_crash(self, plugin_instance):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("SYNC_SERVER_SETTINGS", True)
        with mock.patch("inventree_remote_http_print.plugin.BrotherQLClient") as ClientCls:
            mock_client = ClientCls.return_value
            mock_client.update_settings.side_effect = BrotherQLError("server down")
            # Should not raise.
            plugin_instance.before_printing()


# ---------------------------------------------------------------------------
# print_label workflow
# ---------------------------------------------------------------------------
class TestPrintLabelWorkflow:
    @pytest.fixture
    def png_kwargs(self):
        """Kwargs with both pdf_data and png_file — plugin should prefer PDF."""
        img = Image.new("RGB", (696, 303), color="white")
        return {
            "png_file": img,
            "filename": "stockitem_123",
            "width": 62.0,
            "height": 27.0,
            "label_instance": mock.Mock(name="label_template"),
            "item_instance": mock.Mock(name="stock_item"),
            "user": None,
            "printing_options": {},
            "pdf_data": b"%PDF-1.4 ...",
        }

    @pytest.fixture
    def png_only_kwargs(self):
        """Kwargs with only png_file (no pdf_data) — plugin should fall back to PNG."""
        img = Image.new("RGB", (696, 303), color="white")
        return {
            "png_file": img,
            "filename": "stockitem_123",
            "width": 62.0,
            "height": 27.0,
            "label_instance": mock.Mock(name="label_template"),
            "item_instance": mock.Mock(name="stock_item"),
            "user": None,
            "printing_options": {},
            "pdf_data": None,
        }

    def _patch_client(self, plugin_instance, **method_returns):
        patcher = mock.patch("inventree_remote_http_print.plugin.BrotherQLClient")
        ClientCls = patcher.start()
        client = ClientCls.return_value
        client.upload_file.return_value = method_returns.get("file_id", "f1")
        client.upload_png.return_value = method_returns.get("file_id", "f1")
        client.print.return_value = method_returns.get(
            "queue_item", {"id": "p1", "status": "queued"}
        )
        client.wait_for_completion.return_value = method_returns.get(
            "final", {"id": "p1", "status": "printed"}
        )
        client.TERMINAL_STATUSES = ("printed", "failed")
        return patcher, client

    def test_missing_both_pdf_and_png_raises(self, plugin_instance):
        from django.core.exceptions import ValidationError
        kwargs = {"png_file": None, "pdf_data": None, "printing_options": {}}
        with pytest.raises(ValidationError, match="No label data available"):
            plugin_instance.print_label(**kwargs)

    def test_missing_endpoints_raises(self, plugin_instance, png_kwargs):
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError, match="No print endpoints"):
            plugin_instance.print_label(**png_kwargs)

    def test_happy_path_with_polling(self, plugin_instance, png_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("POLL_STATUS", True)
        plugin_instance.set_setting_for_test("POLL_INTERVAL", 1)
        plugin_instance.set_setting_for_test("POLL_TIMEOUT", 30)

        patcher, client = self._patch_client(plugin_instance)
        try:
            plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()

        # PDF upload happened (preferred over PNG).
        client.upload_file.assert_called_once()
        uploaded_bytes = client.upload_file.call_args[0][0]
        assert uploaded_bytes == b"%PDF-1.4 ..."
        assert client.upload_file.call_args[1]["filename"].endswith(".pdf")
        assert client.upload_file.call_args[1]["content_type"] == "application/pdf"
        client.upload_png.assert_not_called()

        # Print happened with sensible defaults.
        client.print.assert_called_once()
        call = client.print.call_args
        assert call[0][0] == "f1"  # file_id
        assert call[1]["copies"] == 1
        # orientation/label/resize should be None (auto) when no overrides.
        assert call[1]["orientation"] is None
        assert call[1]["label"] is None
        assert call[1]["resize"] is None

        # Polling happened.
        client.wait_for_completion.assert_called_once()
        assert client.wait_for_completion.call_args[0][0] == "p1"
        assert client.wait_for_completion.call_args[1]["poll_interval"] == 1
        assert client.wait_for_completion.call_args[1]["timeout"] == 30

    def test_fire_and_forget_no_polling(self, plugin_instance, png_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("POLL_STATUS", False)

        patcher, client = self._patch_client(plugin_instance)
        try:
            plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()

        client.upload_file.assert_called_once()
        client.print.assert_called_once()
        client.wait_for_completion.assert_not_called()

    def test_dialog_overrides_propagate(self, plugin_instance, png_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        png_kwargs["printing_options"] = {
            "copies": 4,
            "label": "62x29",
            "orientation": "landscape",
            "resize": False,
            "wait_for_completion": False,
        }

        patcher, client = self._patch_client(plugin_instance)
        try:
            plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()

        call = client.print.call_args
        assert call[1]["copies"] == 4
        assert call[1]["label"] == "62x29"
        assert call[1]["orientation"] == "landscape"
        assert call[1]["resize"] is False
        client.wait_for_completion.assert_not_called()

    def test_forced_orientation_enables_resize_by_default(self, plugin_instance, png_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        png_kwargs["printing_options"] = {
            "copies": 1,
            "label": None,
            "orientation": "portrait",
            "resize": None,
            "wait_for_completion": False,
        }
        patcher, client = self._patch_client(plugin_instance)
        try:
            plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()
        assert client.print.call_args[1]["resize"] is True

    def test_upload_error_raises_validation_error(self, plugin_instance, png_kwargs):
        from django.core.exceptions import ValidationError
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        patcher, client = self._patch_client(plugin_instance)
        try:
            client.upload_file.side_effect = BrotherQLError("service unreachable")
            with pytest.raises(ValidationError, match="Could not serialise label"):
                plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()

    def test_print_failure_raises_validation_error(self, plugin_instance, png_kwargs):
        from django.core.exceptions import ValidationError
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("POLL_STATUS", True)

        patcher, client = self._patch_client(plugin_instance)
        try:
            client.wait_for_completion.side_effect = BrotherQLError("out of paper")
            with pytest.raises(ValidationError, match="print job failed"):
                plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()

    def test_poll_timeout_does_not_fail_print(self, plugin_instance, png_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("POLL_STATUS", True)
        plugin_instance.set_setting_for_test("POLL_TIMEOUT", 5)

        patcher, client = self._patch_client(
            plugin_instance,
            final={"id": "p1", "status": "printing"},  # never reached "printed"
        )
        try:
            # Should NOT raise – the job is still running.
            plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()

    def test_filename_gets_pdf_extension(self, plugin_instance, png_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("POLL_STATUS", False)
        png_kwargs["filename"] = "stockitem_123"  # no extension
        patcher, client = self._patch_client(plugin_instance)
        try:
            plugin_instance.print_label(**png_kwargs)
        finally:
            patcher.stop()
        assert client.upload_file.call_args[1]["filename"] == "stockitem_123.pdf"

    def test_filename_gets_png_extension_when_png_fallback(self, plugin_instance, png_only_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("POLL_STATUS", False)
        png_only_kwargs["filename"] = "stockitem_123"  # no extension
        patcher, client = self._patch_client(plugin_instance)
        try:
            plugin_instance.print_label(**png_only_kwargs)
        finally:
            patcher.stop()
        assert client.upload_png.call_args[1]["filename"] == "stockitem_123.png"
        client.upload_file.assert_not_called()

    def test_endpoint_dialog_override(self, plugin_instance, png_kwargs):
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer1.local:8080"},
            {"name": "Warehouse", "url": "http://printer2.local:8080"},
        ])
        png_kwargs["printing_options"] = {"endpoint": "Warehouse"}
        plugin_instance.set_setting_for_test("POLL_STATUS", False)

        with mock.patch("inventree_remote_http_print.plugin.BrotherQLClient") as ClientCls:
            client = ClientCls.return_value
            client.upload_file.return_value = "f1"
            client.print.return_value = {"id": "p1", "status": "queued"}
            client.TERMINAL_STATUSES = ("printed", "failed")
            plugin_instance.print_label(**png_kwargs)

        # BrotherQLClient should have been constructed with the Warehouse URL
        assert ClientCls.call_args[1]["base_url"] == "http://printer2.local:8080"

    def test_png_fallback_when_no_pdf(self, plugin_instance, png_only_kwargs):
        """When pdf_data is None/empty, the plugin should fall back to PNG upload."""
        _set_endpoints(plugin_instance, [
            {"name": "Office", "url": "http://printer.local:8080"},
        ])
        plugin_instance.set_setting_for_test("POLL_STATUS", False)

        patcher, client = self._patch_client(plugin_instance)
        try:
            plugin_instance.print_label(**png_only_kwargs)
        finally:
            patcher.stop()

        client.upload_png.assert_called_once()
        uploaded_bytes = client.upload_png.call_args[0][0]
        assert uploaded_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        assert client.upload_png.call_args[1]["filename"].endswith(".png")
        client.upload_file.assert_not_called()
        client.print.assert_called_once()
