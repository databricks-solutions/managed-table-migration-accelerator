"""Product marker for the Unity Catalog External -> Managed table migration toolkit.

Deliberately tiny and dependency-free. It exists for two reasons:

* It is the single source of the toolkit's product name and version, which
  ``migration.telemetry`` stamps on the Databricks SDK ``User-Agent``
  (``uc-ext2managed/<version>``). The app mirrors ``__version__`` under a parity test.
* Every stage notebook imports it, so library-import tracking can attribute usage to a
  name that is unique to this toolkit (the ``migration`` package name is too generic).

Bump ``__version__`` together with ``app/package.json``, the root entries of
``app/package-lock.json`` and ``VERSION`` in ``app/server/telemetry/product.ts`` on every
release (see "Cutting a release" in ``AGENTS.md``).
"""

PRODUCT = "uc-ext2managed"
__version__ = "0.1.0"
