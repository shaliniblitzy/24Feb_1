"""
Prometheus Metrics Exposition Endpoint

Serves application metrics at ``/service/metrics`` in Prometheus text
exposition format (``text/plain; version=0.0.4; charset=utf-8``).  This
endpoint is the scrape target for Prometheus monitoring infrastructure and
must remain accessible **without authentication** so that the Prometheus
scraper can collect metrics at its configured interval.

Content Negotiation
-------------------
The endpoint inspects the incoming ``Accept`` header.  When the client
requests ``application/openmetrics-text`` (e.g. newer Prometheus builds
or compatible agents), the response switches to the OpenMetrics 1.0.0
exposition format.  Otherwise the standard Prometheus text format is
returned.

Separation of Concerns
----------------------
This module is **separate** from the health-check endpoint located at
``/service/rest/v1/status/check`` (implemented in ``src/admin/health.py``).
This file handles *only* Prometheus metrics exposition — no health logic,
no status aggregation.

Replaces
--------
Dropwizard Metrics 4.2.25 and Prometheus Java client 0.16.0 from the
original Java stack.

Usage
-----
During application factory setup (``src/app.py``)::

    from src.metrics.health import metrics_bp
    app.register_blueprint(metrics_bp)
"""

from flask import Blueprint, Response, request
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, REGISTRY
from prometheus_client.openmetrics.exposition import (
    generate_latest as generate_openmetrics,
)

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------

metrics_bp = Blueprint("metrics", __name__)
"""Flask Blueprint that exposes the ``/service/metrics`` Prometheus scrape
endpoint.  Registered in :func:`src.app.create_app` during application
factory setup."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPENMETRICS_ACCEPT = "application/openmetrics-text"
"""Accept header value that triggers OpenMetrics format negotiation."""

_OPENMETRICS_CONTENT_TYPE = (
    "application/openmetrics-text; version=1.0.0; charset=utf-8"
)
"""Content-Type returned when OpenMetrics format is negotiated."""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@metrics_bp.route("/service/metrics")
def metrics():
    """Serve Prometheus metrics in text exposition format.

    This endpoint is scraped by Prometheus at configurable intervals.
    It serializes **all** registered metrics from the default
    ``prometheus_client`` collector registry.

    **Content Negotiation:**

    * If the ``Accept`` header contains
      ``application/openmetrics-text``, the response uses the
      OpenMetrics 1.0.0 format
      (``application/openmetrics-text; version=1.0.0; charset=utf-8``).
    * Otherwise the standard Prometheus text format is returned
      (``text/plain; version=0.0.4; charset=utf-8``).

    Returns
    -------
    flask.Response
        HTTP 200 with the serialized metrics payload and the
        appropriate ``Content-Type`` header.

    Notes
    -----
    * No authentication is required — Prometheus scrapers need
      unauthenticated access.
    * The metrics output is **not** filtered or transformed — the
      raw Prometheus exposition format is served as-is.
    * Replaces: Dropwizard Metrics 4.2.25 + Prometheus Java client 0.16.0.
    """
    accept_header = request.headers.get("Accept", "")

    if _OPENMETRICS_ACCEPT in accept_header:
        # Client prefers OpenMetrics exposition format.
        # The openmetrics generate_latest requires an explicit registry argument.
        return Response(
            generate_openmetrics(REGISTRY),
            status=200,
            mimetype=_OPENMETRICS_CONTENT_TYPE,
        )

    # Default: standard Prometheus text exposition format.
    return Response(
        generate_latest(),
        status=200,
        mimetype=CONTENT_TYPE_LATEST,
    )
