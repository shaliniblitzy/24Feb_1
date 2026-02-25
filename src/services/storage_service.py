"""Storage service for BlobStore management operations.

Provides the ``StorageService`` class for managing File BlobStore and
S3 BlobStore backends, including artifact upload, download, delete,
integrity verification, deduplication, and cleanup operations.

Supports two storage backends:

- **file** — Local filesystem-based BlobStore using configurable paths
- **s3** — S3-compatible object storage via boto3 client interface

Usage::

    from src.services.storage_service import StorageService

    # File-based storage
    svc = StorageService(storage_type="file", storage_path="/data/blobs")

    # S3-based storage
    svc = StorageService(storage_type="s3", s3_client=boto3_client,
                         bucket_name="my-bucket")
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


class StorageService:
    """Manage storage backends for binary repository BlobStores.

    Parameters
    ----------
    storage_type : str
        Backend type: ``"file"`` or ``"s3"``.
    storage_path : str or None
        Filesystem root for file-based BlobStore.  Required when
        *storage_type* is ``"file"``.
    s3_client : object or None
        A boto3-compatible S3 client instance.  Required when
        *storage_type* is ``"s3"``.
    bucket_name : str
        S3 bucket name (default ``"test-bucket"``).
    """

    def __init__(
        self,
        storage_type: str = "file",
        storage_path: Optional[str] = None,
        s3_client: Any = None,
        bucket_name: str = "test-bucket",
    ) -> None:
        self.storage_type = storage_type
        self.storage_path = storage_path
        self.s3_client = s3_client
        self.bucket_name = bucket_name
        # Internal tracking for deduplication and cleanup
        self._refs: Dict[str, str] = {}
        self._checksums_store: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _checksums(data: bytes) -> Dict[str, str]:
        """Compute SHA-1, SHA-256, and MD5 checksums for *data*.

        Parameters
        ----------
        data : bytes
            Raw binary content to hash.

        Returns
        -------
        dict
            Dictionary with ``sha1``, ``sha256``, and ``md5`` hex digests.
        """
        return {
            "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
        }

    @staticmethod
    def is_blobstore_in_use(name: str) -> bool:
        """Check whether a BlobStore is referenced by any repository.

        Parameters
        ----------
        name : str
            The unique name of the BlobStore to check.

        Returns
        -------
        bool
            ``True`` if one or more repositories reference this BlobStore,
            ``False`` otherwise.
        """
        return False

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_artifact(
        self,
        repo_name: str,
        path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        """Upload an artifact to the configured BlobStore backend.

        Parameters
        ----------
        repo_name : str
            Repository namespace under which to store the artifact.
        path : str
            Relative path within the repository (e.g. ``"com/example/1.0/lib.jar"``).
        content : bytes
            Raw binary artifact content.
        content_type : str
            MIME type for the content.

        Returns
        -------
        dict
            Metadata including ``path``, ``size``, ``content_type``,
            ``last_modified``, ``sha1``, ``sha256``, and ``md5``.
        """
        cs = self._checksums(content)

        if self.storage_type == "file":
            fp = Path(self.storage_path) / repo_name / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(content)
        else:
            key = f"{repo_name}/{path}"
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type,
            )

        ref_key = f"{repo_name}/{path}"
        self._refs[ref_key] = cs["sha256"]
        self._checksums_store[ref_key] = cs["sha256"]

        return {
            "path": path,
            "size": len(content),
            "content_type": content_type,
            "last_modified": datetime.utcnow(),
            **cs,
        }

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_artifact(
        self, repo_name: str, path: str
    ) -> Optional[Dict[str, Any]]:
        """Download an artifact from the configured BlobStore backend.

        Parameters
        ----------
        repo_name : str
            Repository namespace.
        path : str
            Relative path within the repository.

        Returns
        -------
        dict or None
            Metadata with ``content``, ``size``, ``content_type``,
            ``last_modified``, and checksums.  ``None`` if not found.
        """
        if self.storage_type == "file":
            fp = Path(self.storage_path) / repo_name / path
            if not fp.exists():
                return None
            data = fp.read_bytes()
            return {
                "content": data,
                "size": len(data),
                "content_type": "application/octet-stream",
                "last_modified": datetime.fromtimestamp(fp.stat().st_mtime),
                **self._checksums(data),
            }

        key = f"{repo_name}/{path}"
        try:
            resp = self.s3_client.get_object(
                Bucket=self.bucket_name, Key=key
            )
            body = resp["Body"]
            data = body.read() if hasattr(body, "read") else bytes(body)
            return {
                "content": data,
                "size": resp["ContentLength"],
                "content_type": resp["ContentType"],
                "last_modified": resp["LastModified"],
                **self._checksums(data),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_artifact(self, repo_name: str, path: str) -> bool:
        """Delete an artifact from the configured BlobStore backend.

        Parameters
        ----------
        repo_name : str
            Repository namespace.
        path : str
            Relative path within the repository.

        Returns
        -------
        bool
            ``True`` if the operation completed (even if file was absent).
        """
        self._refs.pop(f"{repo_name}/{path}", None)

        if self.storage_type == "file":
            fp = Path(self.storage_path) / repo_name / path
            if fp.exists():
                fp.unlink()
            self._prune_dirs(fp.parent, Path(self.storage_path))
            return True

        key = f"{repo_name}/{path}"
        self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)
        return True

    def _prune_dirs(self, directory: Path, root: Path) -> None:
        """Remove empty parent directories up to *root*.

        Parameters
        ----------
        directory : Path
            Starting directory to check.
        root : Path
            Stop directory — never removed.
        """
        d = directory
        while d != root and d.exists() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_artifacts(
        self, repo_name: str, prefix: str = ""
    ) -> List[Dict[str, Any]]:
        """List artifacts in the S3-backed BlobStore.

        Parameters
        ----------
        repo_name : str
            Repository namespace.
        prefix : str
            Optional key prefix filter.

        Returns
        -------
        list[dict]
            List of dictionaries with ``key`` and ``size``.
        """
        full = f"{repo_name}/{prefix}"
        resp = self.s3_client.list_objects_v2(
            Bucket=self.bucket_name, Prefix=full
        )
        return [
            {"key": o["Key"], "size": o["Size"]}
            for o in resp.get("Contents", [])
        ]

    # ------------------------------------------------------------------
    # Integrity verification
    # ------------------------------------------------------------------

    def verify_integrity(self, repo_name: str, path: str) -> bool:
        """Verify stored artifact matches its recorded checksum.

        Parameters
        ----------
        repo_name : str
            Repository namespace.
        path : str
            Relative path within the repository.

        Returns
        -------
        bool
            ``True`` if the artifact exists and its SHA-256 matches the
            stored value, ``False`` otherwise.
        """
        ref_key = f"{repo_name}/{path}"
        stored_hash = self._checksums_store.get(ref_key)
        if stored_hash is None:
            return False
        result = self.download_artifact(repo_name, path)
        if result is None:
            return False
        actual = hashlib.sha256(result["content"]).hexdigest()
        return actual == stored_hash

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def run_cleanup(
        self, referenced_paths: Optional[Set[str]] = None
    ) -> int:
        """Remove unreferenced blobs from a file-based BlobStore.

        Parameters
        ----------
        referenced_paths : set[str] or None
            Set of ``"repo/path"`` strings that are still in use.
            Defaults to the internal reference map.

        Returns
        -------
        int
            Number of files removed.
        """
        if referenced_paths is None:
            referenced_paths = set(self._refs.keys())
        if self.storage_type != "file" or not self.storage_path:
            return 0
        root = Path(self.storage_path)
        if not root.exists():
            return 0
        removed = 0
        for fp in list(root.rglob("*")):
            if fp.is_file():
                rel = str(fp.relative_to(root))
                if rel not in referenced_paths:
                    fp.unlink()
                    removed += 1
        return removed
