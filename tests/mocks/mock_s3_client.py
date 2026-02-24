"""
Mock S3 Client for BlobStore Operations.

Provides a reusable mock implementation of boto3's S3 client interface that stores
objects entirely in memory. This mock is used by storage service unit tests, integration
tests involving S3 BlobStore, and functional artifact lifecycle tests.

Key features:
- Stateful in-memory object storage with bucket/key hierarchy
- Realistic boto3 response format for all supported operations
- ETag computation using MD5 hex digest (matching real S3 behavior)
- Configurable error injection for testing failure scenarios
- Call logging for method invocation assertions in tests
- Pagination support for list_objects_v2
- Context manager protocol for automatic cleanup

Supported S3 operations:
- put_object: Upload an object to a bucket
- get_object: Retrieve an object with streaming body (BytesIO)
- delete_object: Remove an object from a bucket
- list_objects_v2: List objects with prefix filtering and pagination
- head_object: Retrieve object metadata without body
- create_bucket: Create a new bucket
- delete_bucket: Remove an empty bucket

No real network calls or AWS connectivity are used. All data is stored in
Python dictionaries in memory.
"""

from typing import Dict, Any, Optional, List, BinaryIO
import io
import hashlib
import datetime
import copy

# Guard botocore import for environments without it installed.
# Provides a fallback exception class if botocore is unavailable.
try:
    from botocore.exceptions import ClientError
except ImportError:
    class ClientError(Exception):  # type: ignore[no-redef]
        """Fallback ClientError when botocore is not installed.

        Mimics the botocore.exceptions.ClientError interface with
        response dict and operation_name attributes.
        """

        def __init__(self, error_response: Dict[str, Any], operation_name: str) -> None:
            self.response = error_response
            self.operation_name = operation_name
            error_code = error_response.get('Error', {}).get('Code', 'Unknown')
            error_message = error_response.get('Error', {}).get('Message', '')
            super().__init__(
                f"An error occurred ({error_code}) when calling the "
                f"{operation_name} operation: {error_message}"
            )


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_BUCKET: str = 'test-bucket'
"""Default bucket name pre-created by MockS3Client for convenience in tests."""

MAX_KEYS_DEFAULT: int = 1000
"""Default maximum number of keys returned by list_objects_v2 (matches real S3)."""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def make_client_error(
    error_code: str,
    message: str = '',
    operation_name: str = 'Unknown',
) -> ClientError:
    """Create a properly formatted botocore ClientError instance.

    Factory function that constructs a ClientError with the exact dict
    structure that botocore uses, making it easy to configure error
    injection in MockS3Client without remembering the verbose format.

    Args:
        error_code: The S3 error code string (e.g. 'NoSuchBucket',
            'NoSuchKey', 'AccessDenied', 'BucketNotEmpty', '404').
        message: Human-readable error message. Defaults to an empty string.
        operation_name: The AWS API operation that "failed" (e.g. 'GetObject').

    Returns:
        A ClientError instance ready to be raised or passed to
        ``MockS3Client.configure_error()``.

    Example::

        error = make_client_error('NoSuchKey', 'Not found', 'GetObject')
        mock_client.configure_error('get_object', error)
    """
    error_response: Dict[str, Any] = {
        'Error': {
            'Code': error_code,
            'Message': message,
        },
    }
    return ClientError(error_response, operation_name)


# ---------------------------------------------------------------------------
# MockS3Client class
# ---------------------------------------------------------------------------

class MockS3Client:
    """Mock boto3 S3 client that stores objects in memory.

    Used for testing S3 BlobStore operations without AWS connectivity.
    Objects are stored in a nested dict:
    ``{bucket_name: {object_key: {Body, ContentType, ContentLength, ...}}}``

    The mock supports:

    * **Stateful storage** — ``put_object`` stores bytes in memory;
      ``get_object`` retrieves them wrapped in ``BytesIO``.
    * **Error injection** — Call ``configure_error('method_name', exc)``
      to make any S3 method raise a specific exception on the next call.
    * **Call logging** — Every method invocation is recorded and
      retrievable via ``get_call_log()`` / ``get_call_count()``.
    * **Pagination** — ``list_objects_v2`` honours ``MaxKeys`` and
      ``ContinuationToken`` to mimic real S3 pagination.
    * **Context manager** — Use ``with MockS3Client() as s3:`` for
      automatic cleanup via ``reset()``.

    Attributes:
        _buckets: Nested dictionary mapping bucket names to their objects.
        _call_log: List of dicts recording every method call.
        _error_config: Dict mapping method names to exceptions to raise.
    """

    # ------------------------------------------------------------------
    # Construction / lifecycle
    # ------------------------------------------------------------------

    def __init__(self, **kwargs: Any) -> None:
        """Initialise the mock S3 client.

        Pre-creates the ``DEFAULT_BUCKET`` so tests do not need to call
        ``create_bucket`` for the common case.  Any additional keyword
        arguments are silently accepted (mirroring boto3 client kwargs
        such as ``endpoint_url`` and ``region_name``).

        Args:
            **kwargs: Silently consumed for boto3 interface compatibility.
        """
        self._buckets: Dict[str, Dict[str, dict]] = {}
        self._buckets[DEFAULT_BUCKET] = {}
        self._call_log: List[dict] = []
        self._error_config: Dict[str, Exception] = {}
        # Store kwargs for introspection in tests if needed
        self._init_kwargs: Dict[str, Any] = dict(kwargs)

    def __enter__(self) -> 'MockS3Client':
        """Enter the context manager — returns self."""
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc_val: Any,
        exc_tb: Any,
    ) -> None:
        """Exit the context manager — resets all internal state."""
        self.reset()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_call(
        self,
        method: str,
        args: Optional[Dict[str, Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a method call in the internal call log.

        Args:
            method: Name of the S3 method that was called.
            args: Positional-style arguments captured as a dict.
            kwargs: Additional keyword arguments.
        """
        self._call_log.append({
            'method': method,
            'args': args or {},
            'kwargs': kwargs or {},
            'timestamp': datetime.datetime.utcnow(),
        })

    def _check_error(self, method_name: str) -> None:
        """Raise a configured error for *method_name* if one exists.

        Args:
            method_name: The S3 operation name to check.

        Raises:
            The exception previously registered via ``configure_error``.
        """
        if method_name in self._error_config:
            raise self._error_config[method_name]

    def _require_bucket(self, bucket: str, operation_name: str) -> None:
        """Validate that *bucket* exists; raise ``ClientError`` otherwise.

        Args:
            bucket: Bucket name to verify.
            operation_name: AWS operation name for the error message.

        Raises:
            ClientError: With code ``NoSuchBucket`` if the bucket is missing.
        """
        if bucket not in self._buckets:
            raise make_client_error(
                'NoSuchBucket',
                f'The specified bucket does not exist: {bucket}',
                operation_name,
            )

    @staticmethod
    def _read_body(body: Any) -> bytes:
        """Normalise *body* into ``bytes``.

        Handles ``bytes``, ``str``, and file-like objects (anything with a
        ``read`` method such as ``BytesIO`` or ``BinaryIO``).

        Args:
            body: The raw body content from a ``put_object`` call.

        Returns:
            The body content as ``bytes``.
        """
        if isinstance(body, bytes):
            return body
        if isinstance(body, str):
            return body.encode('utf-8')
        if hasattr(body, 'read'):
            return body.read()
        return bytes(body)

    @staticmethod
    def _compute_etag(data: bytes) -> str:
        """Compute a quoted MD5 hex-digest ETag matching real S3 format.

        Args:
            data: Raw bytes of the object content.

        Returns:
            A string like ``'"d41d8cd98f00b204e9800998ecf8427e"'``.
        """
        md5_hex = hashlib.md5(data).hexdigest()
        return f'"{md5_hex}"'

    # ------------------------------------------------------------------
    # Core S3 operations
    # ------------------------------------------------------------------

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: Any,
        ContentType: str = 'application/octet-stream',
        **kwargs: Any,
    ) -> dict:
        """Store an object in the specified bucket.

        Simulates ``boto3 S3.Client.put_object``.  The object body is
        read into bytes, an MD5-based ETag is computed, and the data is
        stored in the internal ``_buckets`` dict.

        Args:
            Bucket: Target bucket name.
            Key: Object key (path within the bucket).
            Body: Object content — accepts ``bytes``, ``str``, or a
                file-like object with a ``read()`` method.
            ContentType: MIME type. Defaults to ``'application/octet-stream'``.
            **kwargs: Additional metadata (``Metadata``, ``ServerSideEncryption``,
                etc.) stored alongside the object.

        Returns:
            A dict matching the boto3 ``put_object`` response::

                {'ETag': '"<md5hex>"',
                 'ResponseMetadata': {'HTTPStatusCode': 200}}

        Raises:
            ClientError: With code ``NoSuchBucket`` if the bucket is missing.
            Any exception configured via ``configure_error('put_object', ...)``.
        """
        self._log_call('put_object', {
            'Bucket': Bucket,
            'Key': Key,
            'ContentType': ContentType,
        }, kwargs)
        self._check_error('put_object')
        self._require_bucket(Bucket, 'PutObject')

        data = self._read_body(Body)
        etag = self._compute_etag(data)
        now = datetime.datetime.utcnow()

        self._buckets[Bucket][Key] = {
            'Body': data,
            'ContentType': ContentType,
            'ContentLength': len(data),
            'ETag': etag,
            'LastModified': now,
            'Metadata': kwargs.get('Metadata', {}),
        }

        return {
            'ETag': etag,
            'ResponseMetadata': {'HTTPStatusCode': 200},
        }

    def get_object(
        self,
        Bucket: str,
        Key: str,
        **kwargs: Any,
    ) -> dict:
        """Retrieve an object from the specified bucket.

        Simulates ``boto3 S3.Client.get_object``.  The stored bytes are
        wrapped in a ``BytesIO`` to mimic a streaming response body.

        Args:
            Bucket: Source bucket name.
            Key: Object key to retrieve.
            **kwargs: Additional parameters (``Range``, ``VersionId``, etc.)
                accepted for interface compatibility.

        Returns:
            A dict matching the boto3 ``get_object`` response::

                {'Body': BytesIO(...),
                 'ContentType': '...',
                 'ContentLength': 123,
                 'ETag': '"<md5hex>"',
                 'LastModified': datetime(...),
                 'Metadata': {...},
                 'ResponseMetadata': {'HTTPStatusCode': 200}}

        Raises:
            ClientError: ``NoSuchBucket`` if the bucket does not exist.
            ClientError: ``NoSuchKey`` if the key is not found in the bucket.
            Any exception configured via ``configure_error('get_object', ...)``.
        """
        self._log_call('get_object', {
            'Bucket': Bucket,
            'Key': Key,
        }, kwargs)
        self._check_error('get_object')
        self._require_bucket(Bucket, 'GetObject')

        if Key not in self._buckets[Bucket]:
            raise make_client_error(
                'NoSuchKey',
                f'The specified key does not exist: {Key}',
                'GetObject',
            )

        obj = self._buckets[Bucket][Key]
        return {
            'Body': io.BytesIO(copy.deepcopy(obj['Body'])),
            'ContentType': obj['ContentType'],
            'ContentLength': obj['ContentLength'],
            'ETag': obj['ETag'],
            'LastModified': obj['LastModified'],
            'Metadata': copy.deepcopy(obj.get('Metadata', {})),
            'ResponseMetadata': {'HTTPStatusCode': 200},
        }

    def delete_object(
        self,
        Bucket: str,
        Key: str,
        **kwargs: Any,
    ) -> dict:
        """Delete an object from the specified bucket.

        Simulates ``boto3 S3.Client.delete_object``.  Matches real S3
        behaviour: returns 204 even when the key does not exist.

        Args:
            Bucket: Target bucket name.
            Key: Object key to delete.
            **kwargs: Additional parameters accepted for compatibility.

        Returns:
            ``{'ResponseMetadata': {'HTTPStatusCode': 204}}``

        Raises:
            ClientError: ``NoSuchBucket`` if the bucket does not exist.
            Any exception configured via ``configure_error('delete_object', ...)``.
        """
        self._log_call('delete_object', {
            'Bucket': Bucket,
            'Key': Key,
        }, kwargs)
        self._check_error('delete_object')
        self._require_bucket(Bucket, 'DeleteObject')

        # S3 returns 204 regardless of whether the key existed
        self._buckets[Bucket].pop(Key, None)

        return {
            'ResponseMetadata': {'HTTPStatusCode': 204},
        }

    def list_objects_v2(
        self,
        Bucket: str,
        Prefix: str = '',
        MaxKeys: int = MAX_KEYS_DEFAULT,
        ContinuationToken: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """List objects in a bucket with optional prefix filtering and pagination.

        Simulates ``boto3 S3.Client.list_objects_v2``.  Supports
        ``MaxKeys`` truncation and ``ContinuationToken`` for paging
        through large result sets.

        When there are no matching objects the response dict does **not**
        contain a ``Contents`` key, matching real S3 behaviour.

        Args:
            Bucket: Bucket to list.
            Prefix: Only return keys starting with this string.
            MaxKeys: Maximum number of keys to return (default 1000).
            ContinuationToken: Opaque token from a previous truncated
                response (encoded as ``"index:<int>"``).
            **kwargs: Additional parameters (``Delimiter``, ``StartAfter``,
                etc.) accepted for compatibility.

        Returns:
            A dict matching the boto3 ``list_objects_v2`` response.

        Raises:
            ClientError: ``NoSuchBucket`` if the bucket does not exist.
            Any exception configured via ``configure_error('list_objects_v2', ...)``.
        """
        self._log_call('list_objects_v2', {
            'Bucket': Bucket,
            'Prefix': Prefix,
            'MaxKeys': MaxKeys,
            'ContinuationToken': ContinuationToken,
        }, kwargs)
        self._check_error('list_objects_v2')
        self._require_bucket(Bucket, 'ListObjectsV2')

        # Collect and sort matching keys for deterministic pagination
        bucket_data = self._buckets[Bucket]
        matching_keys = sorted(
            key for key in bucket_data if key.startswith(Prefix)
        )

        # Determine start index from continuation token
        start_index = 0
        if ContinuationToken is not None:
            try:
                start_index = int(ContinuationToken.split(':')[-1])
            except (ValueError, IndexError):
                start_index = 0

        # Slice for current page
        page_keys = matching_keys[start_index: start_index + MaxKeys]
        is_truncated = (start_index + MaxKeys) < len(matching_keys)

        result: Dict[str, Any] = {
            'KeyCount': len(page_keys),
            'MaxKeys': MaxKeys,
            'Prefix': Prefix,
            'IsTruncated': is_truncated,
            'ResponseMetadata': {'HTTPStatusCode': 200},
        }

        if is_truncated:
            result['NextContinuationToken'] = f'index:{start_index + MaxKeys}'

        if page_keys:
            result['Contents'] = [
                {
                    'Key': k,
                    'Size': bucket_data[k]['ContentLength'],
                    'ETag': bucket_data[k]['ETag'],
                    'LastModified': bucket_data[k]['LastModified'],
                }
                for k in page_keys
            ]

        return result

    def head_object(
        self,
        Bucket: str,
        Key: str,
        **kwargs: Any,
    ) -> dict:
        """Retrieve metadata for an object without downloading the body.

        Simulates ``boto3 S3.Client.head_object``.

        Args:
            Bucket: Source bucket name.
            Key: Object key.
            **kwargs: Additional parameters accepted for compatibility.

        Returns:
            A dict with ``ContentType``, ``ContentLength``, ``ETag``,
            ``LastModified``, ``Metadata``, and ``ResponseMetadata``.

        Raises:
            ClientError: ``404`` if the bucket or key does not exist.
            Any exception configured via ``configure_error('head_object', ...)``.
        """
        self._log_call('head_object', {
            'Bucket': Bucket,
            'Key': Key,
        }, kwargs)
        self._check_error('head_object')

        if Bucket not in self._buckets:
            raise make_client_error(
                '404',
                f'The specified bucket does not exist: {Bucket}',
                'HeadObject',
            )

        if Key not in self._buckets[Bucket]:
            raise make_client_error(
                '404',
                f'The specified key does not exist: {Key}',
                'HeadObject',
            )

        obj = self._buckets[Bucket][Key]
        return {
            'ContentType': obj['ContentType'],
            'ContentLength': obj['ContentLength'],
            'ETag': obj['ETag'],
            'LastModified': obj['LastModified'],
            'Metadata': copy.deepcopy(obj.get('Metadata', {})),
            'ResponseMetadata': {'HTTPStatusCode': 200},
        }

    def create_bucket(
        self,
        Bucket: str,
        **kwargs: Any,
    ) -> dict:
        """Create a new bucket.

        Simulates ``boto3 S3.Client.create_bucket``.  If the bucket
        already exists the call is a no-op (matching real S3 behaviour
        for the bucket owner).

        Args:
            Bucket: Name of the bucket to create.
            **kwargs: Additional parameters (``CreateBucketConfiguration``,
                etc.) accepted for compatibility.

        Returns:
            ``{'ResponseMetadata': {'HTTPStatusCode': 200}}``
        """
        self._log_call('create_bucket', {
            'Bucket': Bucket,
        }, kwargs)
        self._check_error('create_bucket')

        if Bucket not in self._buckets:
            self._buckets[Bucket] = {}

        return {
            'ResponseMetadata': {'HTTPStatusCode': 200},
        }

    def delete_bucket(
        self,
        Bucket: str,
        **kwargs: Any,
    ) -> dict:
        """Delete an empty bucket.

        Simulates ``boto3 S3.Client.delete_bucket``.  The bucket must
        be empty; otherwise a ``BucketNotEmpty`` error is raised.

        Args:
            Bucket: Name of the bucket to delete.
            **kwargs: Additional parameters accepted for compatibility.

        Returns:
            ``{'ResponseMetadata': {'HTTPStatusCode': 204}}``

        Raises:
            ClientError: ``NoSuchBucket`` if the bucket does not exist.
            ClientError: ``BucketNotEmpty`` if the bucket contains objects.
            Any exception configured via ``configure_error('delete_bucket', ...)``.
        """
        self._log_call('delete_bucket', {
            'Bucket': Bucket,
        }, kwargs)
        self._check_error('delete_bucket')
        self._require_bucket(Bucket, 'DeleteBucket')

        if self._buckets[Bucket]:
            raise make_client_error(
                'BucketNotEmpty',
                f'The bucket you tried to delete is not empty: {Bucket}',
                'DeleteBucket',
            )

        del self._buckets[Bucket]

        return {
            'ResponseMetadata': {'HTTPStatusCode': 204},
        }

    # ------------------------------------------------------------------
    # Test utility methods
    # ------------------------------------------------------------------

    def configure_error(self, method_name: str, error: Exception) -> None:
        """Configure an exception to be raised when *method_name* is called.

        Use this to simulate S3 failures (connection errors, permission
        denied, service unavailable, etc.) in test scenarios.

        Args:
            method_name: Name of the S3 method to intercept (e.g.
                ``'put_object'``, ``'get_object'``).
            error: Exception instance to raise on the next call.

        Example::

            client.configure_error(
                'put_object',
                make_client_error('AccessDenied', 'Access Denied', 'PutObject'),
            )
        """
        self._error_config[method_name] = error

    def clear_error(self, method_name: str) -> None:
        """Remove the configured error for *method_name*.

        After this call the method will resume normal behaviour.

        Args:
            method_name: Name of the S3 method whose error to clear.
        """
        self._error_config.pop(method_name, None)

    def clear_all_errors(self) -> None:
        """Remove all configured errors for all methods."""
        self._error_config.clear()

    def get_call_log(self) -> List[dict]:
        """Return a deep copy of the method call log.

        Each entry is a dict with keys ``method``, ``args``, ``kwargs``,
        and ``timestamp``.  The copy prevents test code from mutating
        internal state.

        Returns:
            List of call-log entry dicts.
        """
        return copy.deepcopy(self._call_log)

    def get_call_count(self, method_name: str) -> int:
        """Return the number of times *method_name* has been called.

        Args:
            method_name: S3 method name to count (e.g. ``'put_object'``).

        Returns:
            Integer call count.
        """
        return sum(1 for entry in self._call_log if entry['method'] == method_name)

    def reset(self) -> None:
        """Reset all internal state to a clean starting point.

        Clears stored objects, the call log, and all error
        configurations.  Re-creates the ``DEFAULT_BUCKET`` so that
        subsequent operations do not need an explicit ``create_bucket``.
        Intended for use in test teardown or ``__exit__``.
        """
        self._buckets.clear()
        self._buckets[DEFAULT_BUCKET] = {}
        self._call_log.clear()
        self._error_config.clear()

    def get_stored_object(
        self,
        bucket: str,
        key: str,
    ) -> Optional[dict]:
        """Directly access stored object data for test assertions.

        Unlike ``get_object`` this does **not** wrap the body in a
        ``BytesIO`` and does **not** log the call.  Returns a deep copy
        to prevent test code from mutating mock state.

        Args:
            bucket: Bucket name.
            key: Object key.

        Returns:
            A deep copy of the stored object dict, or ``None`` if the
            bucket or key does not exist.
        """
        bucket_data = self._buckets.get(bucket)
        if bucket_data is None:
            return None
        obj = bucket_data.get(key)
        if obj is None:
            return None
        return copy.deepcopy(obj)

    def get_stored_objects_count(self, bucket: str = DEFAULT_BUCKET) -> int:
        """Return the number of objects stored in *bucket*.

        Useful for quickly verifying upload and delete operations.

        Args:
            bucket: Bucket name (defaults to ``DEFAULT_BUCKET``).

        Returns:
            Integer count of objects, or ``0`` if the bucket does not exist.
        """
        bucket_data = self._buckets.get(bucket)
        if bucket_data is None:
            return 0
        return len(bucket_data)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_mock_s3_client(**kwargs: Any) -> MockS3Client:
    """Convenience factory for creating a pre-configured MockS3Client.

    Accepts all ``MockS3Client`` constructor kwargs plus:

    * ``bucket_name`` — an additional bucket to pre-create alongside
      ``DEFAULT_BUCKET``.

    Args:
        **kwargs: Passed through to ``MockS3Client.__init__``.  The
            ``bucket_name`` key, if present, is popped and used to
            create an extra bucket.

    Returns:
        A fully initialised ``MockS3Client`` instance.

    Example::

        client = create_mock_s3_client(bucket_name='my-artifacts')
        # client has both 'test-bucket' and 'my-artifacts' buckets ready
    """
    bucket_name: Optional[str] = kwargs.pop('bucket_name', None)
    client = MockS3Client(**kwargs)
    if bucket_name is not None and bucket_name != DEFAULT_BUCKET:
        client.create_bucket(Bucket=bucket_name)
    return client
