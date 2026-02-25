"""
Search index lifecycle management module for the Nexus Repository Flask application.

Implements the Elasticsearch index management portion of Feature F-103
(Content Indexing and Search).  Handles index creation with format-specific
mappings, index rebuilding/reindexing, index deletion, mapping updates, alias
management, and document CRUD operations.

Replaces the Java Elasticsearch 2.4.3 index management from the original
Sonatype Nexus Repository system using the ``elasticsearch-py`` 7.17.12
Python client library.

The module exposes:

- :class:`IndexManager` — main class for all index and document operations
- :func:`initialize_indices` — startup helper called from ``factory.py``
- :data:`COMPONENT_INDEX` / :data:`ASSET_INDEX` — canonical index name constants

All public methods degrade gracefully when Elasticsearch is unavailable,
returning ``False`` or empty results instead of raising exceptions.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from datetime import datetime, timezone

from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import bulk, reindex

from src.app.search.elasticsearch_client import get_es_client

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36 + Logback 1.2.13)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index name constants
# ---------------------------------------------------------------------------
COMPONENT_INDEX: str = "nexus_components"
"""Canonical Elasticsearch index name for component documents."""

ASSET_INDEX: str = "nexus_assets"
"""Canonical Elasticsearch index name for asset documents."""

INDEX_ALIASES: dict[str, str] = {
    "components": COMPONENT_INDEX,
    "assets": ASSET_INDEX,
}
"""Mapping of friendly alias names to their backing index names."""

DEFAULT_NUMBER_OF_SHARDS: int = 1
"""Default number of primary shards for new indices."""

DEFAULT_NUMBER_OF_REPLICAS: int = 0
"""Default number of replica shards (0 for single-node development)."""

BULK_BATCH_SIZE: int = 500
"""Default chunk size for bulk indexing operations."""

# ---------------------------------------------------------------------------
# Index mapping definitions
# ---------------------------------------------------------------------------
COMPONENT_MAPPING: dict[str, Any] = {
    "mappings": {
        "properties": {
            "id": {"type": "integer"},
            "repository_name": {"type": "keyword"},
            "namespace": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
            },
            "name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
                "analyzer": "standard",
            },
            "version": {"type": "keyword"},
            "format": {"type": "keyword"},
            "group": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
            },
            "tags": {"type": "keyword"},
            "attributes": {"type": "object", "enabled": False},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": DEFAULT_NUMBER_OF_SHARDS,
        "number_of_replicas": DEFAULT_NUMBER_OF_REPLICAS,
        "analysis": {
            "analyzer": {
                "component_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "asciifolding"],
                }
            }
        },
    },
}
"""Full Elasticsearch mapping definition for the components index.

Includes text fields with ``.keyword`` sub-fields for exact matching and
aggregations, plus a custom ``component_analyzer`` for tokenisation.
"""

ASSET_MAPPING: dict[str, Any] = {
    "mappings": {
        "properties": {
            "id": {"type": "integer"},
            "component_id": {"type": "integer"},
            "repository_name": {"type": "keyword"},
            "path": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 2048}},
            },
            "content_type": {"type": "keyword"},
            "checksum_sha1": {"type": "keyword"},
            "checksum_sha256": {"type": "keyword"},
            "size": {"type": "long"},
            "format": {"type": "keyword"},
            "last_downloaded": {"type": "date"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": DEFAULT_NUMBER_OF_SHARDS,
        "number_of_replicas": DEFAULT_NUMBER_OF_REPLICAS,
    },
}
"""Full Elasticsearch mapping definition for the assets index."""

INDEX_MAPPINGS: dict[str, dict[str, Any]] = {
    COMPONENT_INDEX: COMPONENT_MAPPING,
    ASSET_INDEX: ASSET_MAPPING,
}
"""Registry that maps canonical index names to their mapping definitions."""


# ═══════════════════════════════════════════════════════════════════════════
# IndexManager class
# ═══════════════════════════════════════════════════════════════════════════
class IndexManager:
    """Elasticsearch index lifecycle management class.

    Provides methods for creating, deleting, and rebuilding indices, managing
    aliases, performing document CRUD, and bulk indexing.  Every public method
    handles Elasticsearch unavailability gracefully — returning ``False`` or
    empty results rather than raising exceptions to callers.

    Parameters
    ----------
    es_client:
        An optional pre-configured ``Elasticsearch`` client instance.  When
        *None* the client is lazily resolved via :func:`get_es_client`.
    """

    def __init__(self, es_client: Optional[Elasticsearch] = None) -> None:
        self._provided_client: Optional[Elasticsearch] = es_client
        logger.info("IndexManager initialised (client=%s)",
                     "provided" if es_client is not None else "lazy")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @property
    def client(self) -> Optional[Elasticsearch]:
        """Resolve the Elasticsearch client, preferring the injected instance
        and falling back to the module-level singleton via
        :func:`get_es_client`.
        """
        if self._provided_client is not None:
            return self._provided_client
        return get_es_client()

    def _ensure_client(self) -> Optional[Elasticsearch]:
        """Return the ES client or ``None`` with a logged warning."""
        es = self.client
        if es is None:
            logger.warning(
                "Elasticsearch client is unavailable — operation skipped"
            )
        return es

    # ------------------------------------------------------------------
    # Index lifecycle methods
    # ------------------------------------------------------------------
    def create_index(
        self,
        index_name: str,
        mapping: Optional[dict[str, Any]] = None,
        ignore_existing: bool = True,
    ) -> bool:
        """Create an Elasticsearch index with the specified mapping.

        Parameters
        ----------
        index_name:
            Name of the index to create.
        mapping:
            Full index body (mappings + settings).  If *None*, the mapping is
            looked up from :data:`INDEX_MAPPINGS`.
        ignore_existing:
            When ``True``, an already-existing index is treated as success.

        Returns
        -------
        bool
            ``True`` on success (or if the index already exists and
            *ignore_existing* is set), ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            if es.indices.exists(index=index_name):
                if ignore_existing:
                    logger.debug(
                        "Index '%s' already exists — skipping creation",
                        index_name,
                    )
                    return True
                logger.warning(
                    "Index '%s' already exists and ignore_existing is False",
                    index_name,
                )
                return False

            body = mapping if mapping is not None else INDEX_MAPPINGS.get(index_name)
            es.indices.create(index=index_name, body=body)
            logger.info("Created index '%s'", index_name)

            # Set up a friendly alias if one is configured for this index.
            for alias_name, target_index in INDEX_ALIASES.items():
                if target_index == index_name:
                    self.create_alias(index_name, alias_name)

            return True
        except Exception as exc:
            logger.error("Failed to create index '%s': %s", index_name, exc)
            return False

    def delete_index(
        self, index_name: str, ignore_missing: bool = True
    ) -> bool:
        """Delete an Elasticsearch index.

        Parameters
        ----------
        index_name:
            Name of the index to delete.
        ignore_missing:
            When ``True``, a missing index is treated as success.

        Returns
        -------
        bool
            ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            if not es.indices.exists(index=index_name):
                if ignore_missing:
                    logger.debug(
                        "Index '%s' does not exist — nothing to delete",
                        index_name,
                    )
                    return True
                logger.warning(
                    "Index '%s' does not exist and ignore_missing is False",
                    index_name,
                )
                return False

            es.indices.delete(index=index_name)
            logger.info("Deleted index '%s'", index_name)
            return True
        except Exception as exc:
            logger.error("Failed to delete index '%s': %s", index_name, exc)
            return False

    def index_exists(self, index_name: str) -> bool:
        """Check whether an index exists.

        Returns ``False`` both when the index is missing *and* when the
        Elasticsearch cluster is unreachable.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            return bool(es.indices.exists(index=index_name))
        except Exception as exc:
            logger.error(
                "Failed to check existence of index '%s': %s",
                index_name,
                exc,
            )
            return False

    def update_mapping(
        self, index_name: str, mapping_update: dict[str, Any]
    ) -> bool:
        """Add new fields to an existing index mapping.

        .. note::
           Elasticsearch does not allow changing the type of existing fields.
           Only additions of new fields are supported.

        Parameters
        ----------
        index_name:
            Target index whose mapping should be updated.
        mapping_update:
            A dictionary of new field definitions to add.

        Returns
        -------
        bool
            ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.indices.put_mapping(index=index_name, body=mapping_update)
            logger.info("Updated mapping for index '%s'", index_name)
            return True
        except Exception as exc:
            logger.error(
                "Failed to update mapping for index '%s': %s",
                index_name,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Alias management
    # ------------------------------------------------------------------
    def create_alias(self, index_name: str, alias_name: str) -> bool:
        """Create an alias pointing to *index_name*.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.indices.put_alias(index=index_name, name=alias_name)
            logger.info(
                "Created alias '%s' -> '%s'", alias_name, index_name
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to create alias '%s' -> '%s': %s",
                alias_name,
                index_name,
                exc,
            )
            return False

    def delete_alias(self, index_name: str, alias_name: str) -> bool:
        """Delete an alias from *index_name*.

        Handles :class:`NotFoundError` gracefully — if the alias does not
        exist, the operation is considered successful.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.indices.delete_alias(index=index_name, name=alias_name)
            logger.info(
                "Deleted alias '%s' from '%s'", alias_name, index_name
            )
            return True
        except NotFoundError:
            logger.debug(
                "Alias '%s' not found on index '%s' — nothing to delete",
                alias_name,
                index_name,
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to delete alias '%s' from '%s': %s",
                alias_name,
                index_name,
                exc,
            )
            return False

    def swap_alias(
        self, alias_name: str, old_index: str, new_index: str
    ) -> bool:
        """Atomically swap *alias_name* from *old_index* to *new_index*.

        This is the key primitive for zero-downtime reindexing — the alias
        points to the old index until the swap completes in a single atomic
        request, then points to the new index.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.indices.update_aliases(
                body={
                    "actions": [
                        {"remove": {"index": old_index, "alias": alias_name}},
                        {"add": {"index": new_index, "alias": alias_name}},
                    ]
                }
            )
            logger.info(
                "Swapped alias '%s': '%s' -> '%s'",
                alias_name,
                old_index,
                new_index,
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to swap alias '%s' from '%s' to '%s': %s",
                alias_name,
                old_index,
                new_index,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Reindexing operations
    # ------------------------------------------------------------------
    def rebuild_index(self, index_name: str) -> bool:
        """Rebuild an index with zero downtime.

        Strategy:

        1. Determine the mapping for the index from :data:`INDEX_MAPPINGS`.
        2. Create a new temporary index with a ``_v<timestamp>`` suffix.
        3. Reindex all documents from the current index into the new one.
        4. Swap any configured alias from the old index to the new one.
        5. Delete the old index.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        new_index_name = f"{index_name}_v{timestamp}"

        try:
            # Step 1 — Create the new index with the same mapping.
            mapping = INDEX_MAPPINGS.get(index_name)
            es.indices.create(index=new_index_name, body=mapping)
            logger.info(
                "Rebuild: created temporary index '%s'", new_index_name
            )

            # Step 2 — Reindex all documents.
            if not self.reindex(index_name, new_index_name):
                logger.error(
                    "Rebuild: reindex from '%s' to '%s' failed",
                    index_name,
                    new_index_name,
                )
                # Attempt cleanup of the new index.
                self.delete_index(new_index_name, ignore_missing=True)
                return False

            # Step 3 — Swap aliases if any are configured.
            for alias_name, target_index in INDEX_ALIASES.items():
                if target_index == index_name:
                    self.swap_alias(alias_name, index_name, new_index_name)

            # Step 4 — Delete the old index.
            es.indices.delete(index=index_name)
            logger.info(
                "Rebuild: deleted old index '%s' after successful reindex",
                index_name,
            )

            # Step 5 — Re-create the canonical index name from the new one
            # by reindexing back and cleaning up the temporary name.  This
            # keeps the canonical name alive for future operations.
            es.indices.create(index=index_name, body=mapping)
            reindex(es, source_index=new_index_name, target_index=index_name)
            es.indices.delete(index=new_index_name, ignore=[400, 404])

            # Restore aliases.
            for alias_name, target_index in INDEX_ALIASES.items():
                if target_index == index_name:
                    self.create_alias(index_name, alias_name)

            logger.info("Rebuild of index '%s' completed successfully", index_name)
            return True
        except Exception as exc:
            logger.error(
                "Failed to rebuild index '%s': %s", index_name, exc
            )
            # Best-effort cleanup of the temporary index.
            self.delete_index(new_index_name, ignore_missing=True)
            return False

    def reindex(self, source_index: str, dest_index: str) -> bool:
        """Reindex all documents from *source_index* into *dest_index*.

        Uses :func:`elasticsearch.helpers.reindex` for efficient bulk transfer.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            logger.info(
                "Reindexing from '%s' to '%s'", source_index, dest_index
            )
            result = reindex(
                es, source_index=source_index, target_index=dest_index
            )
            success_count = result[0] if isinstance(result, tuple) else 0
            logger.info(
                "Reindex complete: %d documents transferred from '%s' to '%s'",
                success_count,
                source_index,
                dest_index,
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to reindex from '%s' to '%s': %s",
                source_index,
                dest_index,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Index statistics and maintenance
    # ------------------------------------------------------------------
    def get_index_stats(self, index_name: str) -> Optional[dict[str, Any]]:
        """Return statistics for the specified index.

        The returned dictionary contains:

        - ``doc_count``: number of documents in the index
        - ``size_in_bytes``: total index size on disk
        - ``deleted_doc_count``: number of deleted (but not yet merged) docs
        - ``primary_store_size``: primary shard storage size

        Returns ``None`` when the Elasticsearch cluster is unavailable or the
        index does not exist.
        """
        es = self._ensure_client()
        if es is None:
            return None

        try:
            stats = es.indices.stats(index=index_name)
            total = stats.get("_all", {}).get("total", {})
            docs = total.get("docs", {})
            store = total.get("store", {})
            return {
                "doc_count": docs.get("count", 0),
                "deleted_doc_count": docs.get("deleted", 0),
                "size_in_bytes": store.get("size_in_bytes", 0),
                "primary_store_size": stats.get("_all", {})
                .get("primaries", {})
                .get("store", {})
                .get("size_in_bytes", 0),
            }
        except Exception as exc:
            logger.error(
                "Failed to retrieve stats for index '%s': %s",
                index_name,
                exc,
            )
            return None

    def refresh_index(self, index_name: str) -> bool:
        """Force-refresh an index so that recent changes become searchable.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.indices.refresh(index=index_name)
            logger.debug("Refreshed index '%s'", index_name)
            return True
        except Exception as exc:
            logger.error(
                "Failed to refresh index '%s': %s", index_name, exc
            )
            return False

    # ------------------------------------------------------------------
    # Document CRUD operations
    # ------------------------------------------------------------------
    def index_document(
        self, index_name: str, doc_id: str, document: dict[str, Any]
    ) -> bool:
        """Index (create or replace) a single document.

        Parameters
        ----------
        index_name:
            Target index.
        doc_id:
            Unique document identifier.
        document:
            Document body as a dictionary.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.index(index=index_name, id=doc_id, body=document)
            logger.debug(
                "Indexed document '%s' in '%s'", doc_id, index_name
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to index document '%s' in '%s': %s",
                doc_id,
                index_name,
                exc,
            )
            return False

    def bulk_index(
        self,
        index_name: str,
        documents: list[dict[str, Any]],
        id_field: str = "id",
    ) -> tuple[int, list[Any]]:
        """Bulk-index multiple documents into the specified index.

        Parameters
        ----------
        index_name:
            Target index.
        documents:
            List of document dictionaries to index.
        id_field:
            Name of the field in each document dict used as the ``_id``.

        Returns
        -------
        tuple[int, list]
            A two-element tuple: ``(success_count, errors_list)``.
            When Elasticsearch is unavailable ``(0, [])`` is returned.
        """
        es = self._ensure_client()
        if es is None:
            return 0, []

        if not documents:
            return 0, []

        actions: list[dict[str, Any]] = [
            {
                "_index": index_name,
                "_id": doc.get(id_field),
                "_source": doc,
            }
            for doc in documents
        ]

        try:
            success_count, errors = bulk(
                es,
                actions,
                chunk_size=BULK_BATCH_SIZE,
                raise_on_error=False,
            )
            if errors:
                logger.warning(
                    "Bulk index into '%s': %d succeeded, %d errors",
                    index_name,
                    success_count,
                    len(errors),
                )
            else:
                logger.info(
                    "Bulk indexed %d documents into '%s'",
                    success_count,
                    index_name,
                )
            return success_count, errors
        except Exception as exc:
            logger.error(
                "Failed to bulk index into '%s': %s", index_name, exc
            )
            return 0, []

    def delete_document(self, index_name: str, doc_id: str) -> bool:
        """Delete a single document by its ID.

        Handles :class:`NotFoundError` gracefully — a missing document is
        treated as a successful deletion.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.delete(index=index_name, id=doc_id)
            logger.debug(
                "Deleted document '%s' from '%s'", doc_id, index_name
            )
            return True
        except NotFoundError:
            logger.debug(
                "Document '%s' not found in '%s' — nothing to delete",
                doc_id,
                index_name,
            )
            return True
        except Exception as exc:
            logger.error(
                "Failed to delete document '%s' from '%s': %s",
                doc_id,
                index_name,
                exc,
            )
            return False

    def update_document(
        self,
        index_name: str,
        doc_id: str,
        partial_doc: dict[str, Any],
    ) -> bool:
        """Partially update an existing document.

        Only the fields present in *partial_doc* are updated; other fields
        remain unchanged.

        Returns ``True`` on success, ``False`` on failure.
        """
        es = self._ensure_client()
        if es is None:
            return False

        try:
            es.update(
                index=index_name,
                id=doc_id,
                body={"doc": partial_doc},
            )
            logger.debug(
                "Updated document '%s' in '%s'", doc_id, index_name
            )
            return True
        except NotFoundError:
            logger.warning(
                "Cannot update document '%s' in '%s' — not found",
                doc_id,
                index_name,
            )
            return False
        except Exception as exc:
            logger.error(
                "Failed to update document '%s' in '%s': %s",
                doc_id,
                index_name,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Component and Asset helper methods
    # ------------------------------------------------------------------
    def index_component(self, component: Any) -> bool:
        """Index a Component model instance into the components index.

        Converts the SQLAlchemy model to a flat dictionary suitable for
        Elasticsearch and delegates to :meth:`index_document`.

        Parameters
        ----------
        component:
            A ``Component`` model instance with attributes ``id``,
            ``repository_name``, ``namespace``, ``name``, ``version``,
            ``repository`` (relationship), ``attributes``, ``created_at``,
            and ``updated_at``.

        Returns ``True`` on success, ``False`` on failure.
        """
        try:
            # Resolve format from the related repository, if loaded.
            repo_format: Optional[str] = None
            if hasattr(component, "repository") and component.repository is not None:
                repo_format = getattr(component.repository, "format", None)

            # Extract tags from the attributes JSON blob.
            tags: list[str] = []
            if hasattr(component, "get_attribute"):
                tags = component.get_attribute("tags", [])
            elif isinstance(getattr(component, "attributes", None), dict):
                tags = component.attributes.get("tags", [])

            doc: dict[str, Any] = {
                "id": component.id,
                "repository_name": getattr(component, "repository_name", None),
                "namespace": getattr(component, "namespace", None),
                "name": getattr(component, "name", None),
                "version": getattr(component, "version", None),
                "format": repo_format,
                "group": getattr(component, "namespace", None),
                "tags": tags,
                "attributes": getattr(component, "attributes", None) or {},
                "created_at": (
                    component.created_at.isoformat()
                    if getattr(component, "created_at", None) is not None
                    else None
                ),
                "updated_at": (
                    component.updated_at.isoformat()
                    if getattr(component, "updated_at", None) is not None
                    else None
                ),
            }

            return self.index_document(COMPONENT_INDEX, str(component.id), doc)
        except Exception as exc:
            logger.error("Failed to index component %s: %s", getattr(component, "id", "?"), exc)
            return False

    def index_asset(self, asset: Any) -> bool:
        """Index an Asset model instance into the assets index.

        Converts the SQLAlchemy model to a flat dictionary suitable for
        Elasticsearch and delegates to :meth:`index_document`.

        Parameters
        ----------
        asset:
            An ``Asset`` model instance with attributes ``id``,
            ``component_id``, ``repository_name``, ``path``,
            ``content_type``, ``checksum_sha1``, ``checksum_sha256``,
            ``size``, ``last_downloaded``, ``created_at``, ``updated_at``,
            and optionally a ``repository`` relationship.

        Returns ``True`` on success, ``False`` on failure.
        """
        try:
            # Resolve format from the related repository, if loaded.
            asset_format: Optional[str] = None
            if hasattr(asset, "repository") and asset.repository is not None:
                asset_format = getattr(asset.repository, "format", None)

            doc: dict[str, Any] = {
                "id": asset.id,
                "component_id": getattr(asset, "component_id", None),
                "repository_name": getattr(asset, "repository_name", None),
                "path": getattr(asset, "path", None),
                "content_type": getattr(asset, "content_type", None),
                "checksum_sha1": getattr(asset, "checksum_sha1", None),
                "checksum_sha256": getattr(asset, "checksum_sha256", None),
                "size": getattr(asset, "size", None),
                "format": asset_format,
                "last_downloaded": (
                    asset.last_downloaded.isoformat()
                    if getattr(asset, "last_downloaded", None) is not None
                    else None
                ),
                "created_at": (
                    asset.created_at.isoformat()
                    if getattr(asset, "created_at", None) is not None
                    else None
                ),
                "updated_at": (
                    asset.updated_at.isoformat()
                    if getattr(asset, "updated_at", None) is not None
                    else None
                ),
            }

            return self.index_document(ASSET_INDEX, str(asset.id), doc)
        except Exception as exc:
            logger.error("Failed to index asset %s: %s", getattr(asset, "id", "?"), exc)
            return False

    def remove_component(self, component_id: int) -> bool:
        """Remove a component document from the search index.

        Parameters
        ----------
        component_id:
            Primary key of the component to remove.

        Returns ``True`` on success, ``False`` on failure.
        """
        return self.delete_document(COMPONENT_INDEX, str(component_id))

    def remove_asset(self, asset_id: int) -> bool:
        """Remove an asset document from the search index.

        Parameters
        ----------
        asset_id:
            Primary key of the asset to remove.

        Returns ``True`` on success, ``False`` on failure.
        """
        return self.delete_document(ASSET_INDEX, str(asset_id))


# ═══════════════════════════════════════════════════════════════════════════
# Module-level initialisation helper
# ═══════════════════════════════════════════════════════════════════════════

def initialize_indices() -> bool:
    """Create all required Elasticsearch indices if they do not already exist.

    This function is intended to be called once during application startup
    (from ``factory.py``).  It creates the ``nexus_components`` and
    ``nexus_assets`` indices with their predefined mappings and configures
    friendly aliases.

    The function handles Elasticsearch unavailability gracefully — if the
    cluster is unreachable, a warning is logged and ``False`` is returned so
    that the Flask application can continue to operate with search features
    degraded.

    Returns
    -------
    bool
        ``True`` when all indices are verified/created successfully, ``False``
        when any index creation fails or when Elasticsearch is unavailable.
    """
    manager = IndexManager()

    # Quick availability check.
    if manager.client is None:
        logger.warning(
            "Elasticsearch is unavailable — skipping index initialisation. "
            "Search features will be degraded."
        )
        return False

    all_ok = True

    for index_name, mapping in INDEX_MAPPINGS.items():
        if not manager.create_index(index_name, mapping=mapping, ignore_existing=True):
            logger.error("Failed to create/verify index '%s'", index_name)
            all_ok = False

    if all_ok:
        logger.info(
            "All search indices initialised successfully: %s",
            ", ".join(INDEX_MAPPINGS.keys()),
        )
    else:
        logger.warning("Some search indices failed to initialise")

    return all_ok
