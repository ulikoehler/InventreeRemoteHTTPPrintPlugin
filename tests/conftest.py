"""Pytest configuration for the test suite.

These tests use the `responses` library to mock the `requests` HTTP layer, so
they run without an InvenTree install, a BrotherQL service, or a real
printer. The goal is to verify the plugin's HTTP workflow logic, not the
InvenTree plugin framework itself (that's tested by InvenTree's own suite).
"""

import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Stub out the InvenTree / Django / DRF imports so the plugin module can be
# imported in a clean Python environment without a full InvenTree install.
# This lets us unit-test the plugin's option-resolution and HTTP workflow
# logic in isolation.
# ---------------------------------------------------------------------------
def _install_inventree_stubs() -> None:
    # --- Django stubs ---
    django = types.ModuleType("django")
    django_exceptions = types.ModuleType("django.core.exceptions")
    class ValidationError(Exception):
        def __init__(self, message="", *args, **kwargs):
            super().__init__(message)
            self.message = message
    django_exceptions.ValidationError = ValidationError
    django_core = types.ModuleType("django.core")
    django_core.exceptions = django_exceptions
    django.core = django_core

    django_utils = types.ModuleType("django.utils")
    django_utils_translation = types.ModuleType("django.utils.translation")

    def gettext_lazy(s):
        return s
    django_utils_translation.gettext_lazy = gettext_lazy
    django_utils.translation = django_utils_translation
    django.utils = django_utils

    sys.modules.setdefault("django", django)
    sys.modules.setdefault("django.core", django_core)
    sys.modules.setdefault("django.core.exceptions", django_exceptions)
    sys.modules.setdefault("django.utils", django_utils)
    sys.modules.setdefault("django.utils.translation", django_utils_translation)

    # --- DRF stubs (just enough serializer base classes) ---
    rest_framework = types.ModuleType("rest_framework")
    class _Field:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
    class Serializer:
        def __init__(self, *args, **kwargs):
            self.initial_data = kwargs.get("data")
        def is_valid(self, *args, **kwargs):
            return True
        @property
        def data(self):
            return self.initial_data or {}
    class IntegerField(_Field): pass
    class CharField(_Field): pass
    class ChoiceField(_Field): pass
    class BooleanField(_Field): pass
    rest_framework.serializers = types.SimpleNamespace(
        Serializer=Serializer,
        IntegerField=IntegerField,
        CharField=CharField,
        ChoiceField=ChoiceField,
        BooleanField=BooleanField,
    )
    sys.modules.setdefault("rest_framework", rest_framework)
    sys.modules.setdefault("rest_framework.serializers", rest_framework.serializers)

    # --- InvenTree plugin framework stubs ---
    plugin = types.ModuleType("plugin")
    class InvenTreePlugin:
        # Mirror the metaclass behaviour loosely: settings accessor + metadata.
        NAME = "BasePlugin"
        SLUG = "base"
        TITLE = "Base Plugin"
        DESCRIPTION = ""
        AUTHOR = ""
        VERSION = "0.0.0"
        def __init__(self):
            self._settings_overrides = {}
        def get_setting(self, key, cache=True, backup_value=None):
            if key in self._settings_overrides:
                return self._settings_overrides[key]
            return self._default_setting(key, backup_value)
        def set_setting_for_test(self, key, value):
            """Test-only helper to inject settings without a DB."""
            self._settings_overrides[key] = value
        def _default_setting(self, key, backup_value):
            # Look up SETTINGS[key]['default'] if defined.
            settings = getattr(self, "SETTINGS", {})
            entry = settings.get(key, {})
            if "default" in entry:
                return entry["default"]
            return backup_value
    plugin.InvenTreePlugin = InvenTreePlugin

    plugin_mixins = types.ModuleType("plugin.mixins")
    class LabelPrintingMixin:
        BLOCKING_PRINT = True
        def before_printing(self): pass
        def after_printing(self): pass
        def print_label(self, **kwargs):  # abstract in real impl
            raise NotImplementedError
    class SettingsMixin:
        pass
    plugin_mixins.LabelPrintingMixin = LabelPrintingMixin
    plugin_mixins.SettingsMixin = SettingsMixin
    sys.modules.setdefault("plugin", plugin)
    sys.modules.setdefault("plugin.mixins", plugin_mixins)


_install_inventree_stubs()


@pytest.fixture
def plugin_instance():
    """Fresh plugin instance with default settings (overridable per-test)."""
    from inventree_remote_http_print.plugin import RemoteHTTPPrintServicePlugin
    return RemoteHTTPPrintServicePlugin()
