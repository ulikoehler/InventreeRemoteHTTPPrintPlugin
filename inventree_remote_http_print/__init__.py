"""Remote HTTP print service — InvenTree label-printing plugin.

This package exposes a single InvenTree plugin class, ``RemoteHTTPPrintServicePlugin``,
which sends rendered label PNGs to a running instance of the
BrotherQL Label Print Service (https://github.com/ulikoehler/BrotherQLLabelPrintService)
or any compatible remote HTTP print service.
"""

# Package version – also used as the InvenTree plugin version (see plugin.py).
PLUGIN_VERSION = "1.0.0"
