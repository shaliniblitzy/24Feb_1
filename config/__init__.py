"""
Configuration package for the Nexus Repository Flask application.

Provides environment-aware configuration selection via the :func:`get_config`
function and :data:`config_dict` dictionary.  The application factory
(``src/app/factory.py``) calls ``get_config()`` to resolve the active
configuration class based on the ``FLASK_CONFIG`` environment variable,
following the twelve-factor app methodology for environment-based
configuration selection.

This module replaces the Java System Configuration (F-404) initialization
logic from the original Sonatype Nexus Repository server.

Available configurations
------------------------
``default``
    Base configuration with sensible development-safe defaults.
``development``
    SQLite database, debug mode enabled, verbose SQL logging.
``production``
    PostgreSQL database, strict security (mandatory SECRET_KEY), JSON
    structured logging, optimized connection pooling.
``testing``
    In-memory SQLite, deterministic security keys, disabled external
    services (Elasticsearch, LDAP, SSO), reduced bcrypt rounds.

Usage::

    from config import get_config, config_dict

    # Auto-detect from FLASK_CONFIG env var (defaults to 'development')
    config_class = get_config()

    # Explicit selection
    config_class = get_config('testing')

    # Dictionary access
    config_class = config_dict['production']
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Import configuration classes from sibling modules
# ---------------------------------------------------------------------------
# DefaultConfig, DevelopmentConfig, and TestingConfig are safe to import
# unconditionally — they use os.getenv() with sensible defaults throughout.
from config.default import DefaultConfig
from config.development import DevelopmentConfig
from config.testing import TestingConfig

# ProductionConfig accesses ``os.environ['SECRET_KEY']`` at class-definition
# time as a deliberate fail-fast design choice: production deployments MUST
# have a properly generated secret key, and missing it should be caught
# immediately at startup rather than at the first request that needs it.
#
# To allow the config package to be loaded in development and testing
# environments where the SECRET_KEY environment variable may not be
# present, the import is wrapped in a try/except.  When SECRET_KEY IS
# available the real ProductionConfig is imported with all its hardened
# settings; otherwise a lightweight stand-in that inherits DefaultConfig
# is created so that ``config_dict`` and ``__all__`` remain structurally
# complete and the package can still be imported without errors.
try:
    from config.production import ProductionConfig
except KeyError:

    class ProductionConfig(DefaultConfig):  # type: ignore[no-redef]
        """Deferred ProductionConfig placeholder.

        The real ``ProductionConfig`` in ``config/production.py`` requires
        the ``SECRET_KEY`` environment variable to be set at import time.
        This stand-in is created only when ``SECRET_KEY`` is absent (i.e.,
        in non-production environments).  Attempting to run the application
        with this placeholder will **not** provide production-grade
        security settings — set ``SECRET_KEY`` to load the full
        production configuration.
        """

        ENV: str = "production"
        _DEFERRED: bool = True


# ---------------------------------------------------------------------------
# Configuration Registry
# ---------------------------------------------------------------------------

config_dict: dict[str, type[DefaultConfig]] = {
    "default": DefaultConfig,
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
"""Mapping of configuration name to configuration class.

Used by ``create_app(config_name)`` in ``src/app/factory.py`` to
resolve the active configuration.  Keys correspond to the values
accepted by the ``FLASK_CONFIG`` environment variable.

Members exposed:
    - ``default``     — :class:`~config.default.DefaultConfig`
    - ``development`` — :class:`~config.development.DevelopmentConfig`
    - ``production``  — :class:`~config.production.ProductionConfig`
    - ``testing``     — :class:`~config.testing.TestingConfig`
"""


# ---------------------------------------------------------------------------
# Configuration Selector
# ---------------------------------------------------------------------------


def get_config(config_name: str | None = None) -> type[DefaultConfig]:
    """Return the configuration class for the requested environment.

    Reads the ``FLASK_CONFIG`` environment variable when *config_name* is
    not supplied, defaulting to ``'development'`` when the variable is
    unset.  This enables seamless environment-based configuration switching
    per the twelve-factor app methodology.

    Parameters
    ----------
    config_name : str or None
        One of ``'default'``, ``'development'``, ``'production'``, or
        ``'testing'``.  When ``None``, the value is read from the
        ``FLASK_CONFIG`` environment variable (falling back to
        ``'development'`` if unset).

    Returns
    -------
    type[DefaultConfig]
        The configuration **class** (not an instance).  The caller is
        responsible for passing it to ``app.config.from_object()`` or
        instantiating it as needed.

    Raises
    ------
    ValueError
        If *config_name* does not match any key in :data:`config_dict`.

    Examples
    --------
    >>> cfg = get_config()            # reads FLASK_CONFIG or defaults to 'development'
    >>> cfg = get_config('testing')   # explicit selection
    """
    if config_name is None:
        config_name = os.getenv("FLASK_CONFIG", "development")

    config_class = config_dict.get(config_name)
    if config_class is None:
        available = ", ".join(sorted(config_dict.keys()))
        raise ValueError(
            f"Unknown configuration '{config_name}'. "
            f"Available configurations: {available}"
        )
    return config_class


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "config_dict",
    "get_config",
    "DefaultConfig",
    "DevelopmentConfig",
    "ProductionConfig",
    "TestingConfig",
]
