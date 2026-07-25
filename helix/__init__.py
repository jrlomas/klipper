"""Portable HELIX machine-module source API.

This package is intentionally independent of Klippy.  Portable application
source imports it both when executed by the host reference runtime and when
inspected by ``helixc`` for native compilation.

Only the first, integer stateful-actor slice is implemented today.  The
package version therefore describes the source contract under development,
not a claim that the complete FD-0001 API is available.
"""

SOURCE_API_VERSION = "0.1"
