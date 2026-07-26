"""Security primitives shared across configuration, startup and request paths.

Modules here must stay importable from `config.py` itself, so nothing in this
package may import `config` at module scope.
"""
