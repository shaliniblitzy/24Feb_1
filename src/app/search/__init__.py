"""
Search package for the Nexus Repository Flask application.

Provides Elasticsearch integration for content indexing and full-text search
(Feature F-103). Replaces the Java Elasticsearch 2.4.3 client from the
original Sonatype Nexus Repository system.

Modules:
- ``elasticsearch_client.py``: Connection management and health checking
- ``index_manager.py``:        Index lifecycle operations (future checkpoint)
- ``query_builder.py``:        Search query construction (future checkpoint)
"""

from src.app.search.elasticsearch_client import ElasticsearchClient  # noqa: F401
