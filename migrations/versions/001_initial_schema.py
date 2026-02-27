"""Initial database schema - create all entity tables.

Revision ID: 001
Revises: (None — this is the first migration)
Create Date: 2025-02-24

Creates the complete initial database schema for the Nexus Repository
Python/Flask application. This migration defines all 14 entity tables
from the DataStore schema, replacing Flyway 8.5.13 versioned migration
scripts from the Java source system.

Tables created (in dependency order):
  1. repositories       - Multi-format repository definitions (F-101, F-102)
  2. users              - User accounts and credentials (F-301)
  3. roles              - Role definitions with privilege aggregation (F-301)
  4. role_assignments   - User-to-role junction table (F-301)
  5. privileges         - Privilege descriptors (F-301)
  6. content_selectors  - CSEL/JEXL content selector expressions (F-301)
  7. components         - Artifact components with coordinates (F-101, F-103)
  8. assets             - Binary assets with checksums (F-101, F-103, F-204)
  9. task_definitions   - Scheduled task configurations (F-402)
 10. task_executions    - Task execution history (F-402)
 11. system_configs     - System configuration key-value store (F-404)
 12. audit_events       - Immutable audit log entries (F-303)
 13. blobstore_configs  - BlobStore backend configurations (F-201, F-202)
 14. cleanup_policies   - Cleanup policy rule definitions (F-204)

Dual-database compatible: Works on both SQLite (standalone) and
PostgreSQL (clustered) deployments via portable SQLAlchemy types
and server defaults.
"""

from alembic import op
import sqlalchemy as sa

# Alembic revision identifiers — used by the migration framework
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all 14 entity tables for the initial database schema.

    Tables are created in dependency order: independent tables first,
    then tables with foreign key references to already-created tables.
    Indexes are created immediately after each table for optimal
    query performance.
    """

    # ──────────────────────────────────────────────────────────────────────
    # 1. repositories — Multi-format repository definitions (independent)
    # Replaces: REPOSITORY entity from DataStore schema (Section 6.2.1.2)
    # Features: F-101 (Multi-Format), F-102 (Repository Types), F-404 (Config)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'repositories',
        sa.Column('name', sa.String(200), primary_key=True),
        sa.Column('format', sa.String(50), nullable=False),
        sa.Column('type', sa.String(20), nullable=False),
        sa.Column('blob_store_name', sa.String(200), nullable=False),
        sa.Column('online', sa.Boolean(), nullable=False,
                  server_default=sa.text('1')),
        sa.Column('routing_rule', sa.String(200), nullable=True),
        sa.Column('cleanup_policies', sa.JSON(), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False,
                  server_default=sa.text('0')),
    )
    op.create_index('ix_repositories_format', 'repositories', ['format'])
    op.create_index('ix_repositories_type', 'repositories', ['type'])
    op.create_index('ix_repositories_format_type', 'repositories',
                    ['format', 'type'])
    op.create_index('ix_repositories_online', 'repositories', ['online'])

    # ──────────────────────────────────────────────────────────────────────
    # 2. users — User accounts and credentials (independent)
    # Replaces: USER entity from DataStore schema
    # Features: F-301 (RBAC), F-304 (API Key Authentication)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'users',
        sa.Column('user_id', sa.String(200), primary_key=True),
        sa.Column('password_hash', sa.String(512), nullable=True),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='active'),
        sa.Column('email', sa.String(255), nullable=True),
        sa.Column('first_name', sa.String(100), nullable=True),
        sa.Column('last_name', sa.String(100), nullable=True),
        sa.Column('api_key', sa.String(255), nullable=True, unique=True),
        sa.Column('external_id', sa.String(500), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.Column('failed_login_count', sa.Integer(), nullable=True,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False,
                  server_default=sa.text('0')),
    )
    op.create_index('ix_users_email', 'users', ['email'])
    op.create_index('ix_users_status', 'users', ['status'])
    op.create_index('ix_users_external_id', 'users', ['external_id'])
    op.create_index('ix_users_api_key', 'users', ['api_key'], unique=True)

    # ──────────────────────────────────────────────────────────────────────
    # 3. roles — Role definitions with privilege aggregation (independent)
    # Replaces: ROLE entity from DataStore schema
    # Features: F-301 (RBAC)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'roles',
        sa.Column('role_id', sa.String(200), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False, unique=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('privileges', sa.JSON(), nullable=True),
        sa.Column('source', sa.String(50), nullable=False,
                  server_default='internal'),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_roles_source', 'roles', ['source'])

    # ──────────────────────────────────────────────────────────────────────
    # 4. role_assignments — User-to-role junction (depends on: users, roles)
    # Replaces: ROLE_ASSIGNMENT junction table from DataStore schema
    # Features: F-301 (RBAC)
    # Composite PK: (user_id, role_id) — each user-role pair is unique
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'role_assignments',
        sa.Column('user_id', sa.String(200),
                  sa.ForeignKey('users.user_id'), primary_key=True),
        sa.Column('role_id', sa.String(200),
                  sa.ForeignKey('roles.role_id'), primary_key=True),
        sa.Column('created_at', sa.DateTime(), nullable=True,
                  server_default=sa.func.now()),
    )

    # ──────────────────────────────────────────────────────────────────────
    # 5. privileges — Privilege descriptor definitions (independent)
    # Replaces: PRIVILEGE entity from DataStore schema
    # Features: F-301 (RBAC)
    # Types: application, repository-admin, repository-content-selector,
    #        repository-view, wildcard
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'privileges',
        sa.Column('privilege_id', sa.String(300), primary_key=True),
        sa.Column('type', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('properties', sa.JSON(), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_privileges_type', 'privileges', ['type'])
    op.create_index('ix_privileges_name', 'privileges', ['name'])

    # ──────────────────────────────────────────────────────────────────────
    # 6. content_selectors — CSEL/JEXL expression definitions (independent)
    # Replaces: CONTENT_SELECTOR entity from DataStore schema
    # Features: F-301 (RBAC — sub-repository authorization via CSEL)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'content_selectors',
        sa.Column('selector_id', sa.String(200), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False, unique=True),
        sa.Column('type', sa.String(20), nullable=False,
                  server_default='csel'),
        sa.Column('expression', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_content_selectors_type', 'content_selectors',
                    ['type'])

    # ──────────────────────────────────────────────────────────────────────
    # 7. components — Artifact components with coordinates
    #    (depends on: repositories)
    # Replaces: COMPONENT entity from DataStore schema
    # Features: F-101 (Multi-Format), F-103 (Content Indexing)
    # Unique constraint on (repository_name, namespace, name, version)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'components',
        sa.Column('id', sa.Integer(), primary_key=True,
                  autoincrement=True),
        sa.Column('repository_name', sa.String(200),
                  sa.ForeignKey('repositories.name'), nullable=False),
        sa.Column('namespace', sa.String(512), nullable=True),
        sa.Column('name', sa.String(512), nullable=False),
        sa.Column('version', sa.String(256), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False,
                  server_default=sa.text('0')),
        # Unique constraint on component coordinates — defined inline for
        # SQLite compatibility (SQLite does not support ALTER TABLE ADD CONSTRAINT)
        sa.UniqueConstraint('repository_name', 'namespace', 'name', 'version',
                            name='uq_component_coordinates'),
    )
    op.create_index('ix_components_repository_name', 'components',
                    ['repository_name'])
    op.create_index('ix_components_repo_name_version', 'components',
                    ['repository_name', 'name', 'version'])
    op.create_index('ix_components_namespace', 'components', ['namespace'])

    # ──────────────────────────────────────────────────────────────────────
    # 8. assets — Binary assets with checksums and BlobStore references
    #    (depends on: repositories, components)
    # Replaces: ASSET entity from DataStore schema
    # Features: F-101 (Multi-Format), F-103 (Indexing), F-204 (Cleanup)
    # Unique path per repository: (repository_name, path)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'assets',
        sa.Column('id', sa.BigInteger(), primary_key=True,
                  autoincrement=True),
        sa.Column('component_id', sa.Integer(),
                  sa.ForeignKey('components.id'), nullable=True),
        sa.Column('repository_name', sa.String(200),
                  sa.ForeignKey('repositories.name'), nullable=False),
        sa.Column('path', sa.String(2048), nullable=False),
        sa.Column('content_type', sa.String(255), nullable=True),
        sa.Column('checksum_sha1', sa.String(40), nullable=True),
        sa.Column('checksum_sha256', sa.String(64), nullable=True),
        sa.Column('checksum_md5', sa.String(32), nullable=True),
        sa.Column('size', sa.BigInteger(), nullable=True),
        sa.Column('last_downloaded', sa.DateTime(), nullable=True),
        sa.Column('blob_ref', sa.String(500), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False,
                  server_default=sa.text('0')),
    )
    op.create_index('ix_assets_repo_path', 'assets',
                    ['repository_name', 'path'], unique=True)
    op.create_index('ix_assets_last_downloaded', 'assets',
                    ['last_downloaded'])
    op.create_index('ix_assets_content_type', 'assets', ['content_type'])
    op.create_index('ix_assets_component_id', 'assets', ['component_id'])

    # ──────────────────────────────────────────────────────────────────────
    # 9. task_definitions — Scheduled task configurations (independent)
    # Replaces: TASK_DEFINITION entity from DataStore schema
    # Features: F-402 (Scheduled Tasks)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'task_definitions',
        sa.Column('task_id', sa.String(200), primary_key=True),
        sa.Column('type', sa.String(100), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('cron_expression', sa.String(100), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False,
                  server_default=sa.text('1')),
        sa.Column('configuration', sa.JSON(), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('last_run_status', sa.String(50), nullable=True),
        sa.Column('next_run_time', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_task_definitions_type', 'task_definitions', ['type'])
    op.create_index('ix_task_definitions_enabled', 'task_definitions',
                    ['enabled'])

    # ──────────────────────────────────────────────────────────────────────
    # 10. task_executions — Task execution history
    #     (depends on: task_definitions)
    # Replaces: TASK_EXECUTION entity from DataStore schema
    # Features: F-402 (Scheduled Tasks)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'task_executions',
        sa.Column('execution_id', sa.BigInteger(), primary_key=True,
                  autoincrement=True),
        sa.Column('task_id', sa.String(200),
                  sa.ForeignKey('task_definitions.task_id'), nullable=False),
        sa.Column('start_time', sa.DateTime(), nullable=False),
        sa.Column('end_time', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(50), nullable=False,
                  server_default='waiting'),
        sa.Column('duration_ms', sa.BigInteger(), nullable=True),
        sa.Column('progress', sa.String(255), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_task_executions_task_id', 'task_executions',
                    ['task_id'])
    op.create_index('ix_task_executions_task_id_start', 'task_executions',
                    ['task_id', 'start_time'])
    op.create_index('ix_task_executions_status', 'task_executions',
                    ['status'])

    # ──────────────────────────────────────────────────────────────────────
    # 11. system_configs — System configuration key-value store (independent)
    # Replaces: SYSTEM_CONFIG entity from DataStore schema
    # Features: F-404 (System Configuration Management)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'system_configs',
        sa.Column('key', sa.String(500), primary_key=True),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('category', sa.String(100), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_system_configs_category', 'system_configs',
                    ['category'])

    # ──────────────────────────────────────────────────────────────────────
    # 12. audit_events — Immutable audit log entries (independent)
    # Replaces: AUDIT_EVENT entity from DataStore schema
    # Features: F-303 (Audit Logging)
    # High-volume table — multiple indexes for query performance
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'audit_events',
        sa.Column('event_id', sa.BigInteger(), primary_key=True,
                  autoincrement=True),
        sa.Column('user_id', sa.String(200), nullable=True),
        sa.Column('event_type', sa.String(100), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('domain', sa.String(100), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('node_id', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_audit_events_user_id', 'audit_events', ['user_id'])
    op.create_index('ix_audit_events_event_type', 'audit_events',
                    ['event_type'])
    op.create_index('ix_audit_events_timestamp', 'audit_events',
                    ['timestamp'])
    op.create_index('ix_audit_events_domain', 'audit_events', ['domain'])
    op.create_index('ix_audit_events_user_timestamp', 'audit_events',
                    ['user_id', 'timestamp'])

    # ──────────────────────────────────────────────────────────────────────
    # 13. blobstore_configs — BlobStore backend configurations (independent)
    # Replaces: BLOBSTORE_CONFIG entity from DataStore schema
    # Features: F-201 (File BlobStore), F-202 (S3 BlobStore)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'blobstore_configs',
        sa.Column('blob_store_name', sa.String(200), primary_key=True),
        sa.Column('type', sa.String(20), nullable=False),
        sa.Column('configuration', sa.JSON(), nullable=False),
        sa.Column('total_size', sa.BigInteger(), nullable=True,
                  server_default='0'),
        sa.Column('blob_count', sa.BigInteger(), nullable=True,
                  server_default='0'),
        sa.Column('available_space', sa.BigInteger(), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_blobstore_configs_type', 'blobstore_configs',
                    ['type'])

    # ──────────────────────────────────────────────────────────────────────
    # 14. cleanup_policies — Cleanup policy rule definitions (independent)
    # Replaces: CLEANUP_POLICY entity from DataStore schema
    # Features: F-204 (Cleanup Policies)
    # ──────────────────────────────────────────────────────────────────────
    op.create_table(
        'cleanup_policies',
        sa.Column('policy_id', sa.String(200), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False, unique=True),
        sa.Column('format', sa.String(50), nullable=True),
        sa.Column('criteria', sa.JSON(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_cleanup_policies_format', 'cleanup_policies',
                    ['format'])


def downgrade() -> None:
    """Drop all 14 entity tables in reverse dependency order.

    Tables with foreign key references are dropped first to avoid
    constraint violations. Indexes are explicitly dropped before
    their parent tables for clean removal.
    """

    # ── Drop FK-dependent tables first ─────────────────────────────────

    # 10. task_executions (FK → task_definitions)
    op.drop_index('ix_task_executions_status',
                  table_name='task_executions')
    op.drop_index('ix_task_executions_task_id_start',
                  table_name='task_executions')
    op.drop_index('ix_task_executions_task_id',
                  table_name='task_executions')
    op.drop_table('task_executions')

    # 8. assets (FK → components, repositories)
    op.drop_index('ix_assets_component_id', table_name='assets')
    op.drop_index('ix_assets_content_type', table_name='assets')
    op.drop_index('ix_assets_last_downloaded', table_name='assets')
    op.drop_index('ix_assets_repo_path', table_name='assets')
    op.drop_table('assets')

    # 7. components (FK → repositories)
    op.drop_index('ix_components_namespace', table_name='components')
    op.drop_index('ix_components_repo_name_version',
                  table_name='components')
    op.drop_index('ix_components_repository_name',
                  table_name='components')
    op.drop_table('components')

    # 4. role_assignments (FK → users, roles)
    op.drop_table('role_assignments')

    # ── Drop independent tables ────────────────────────────────────────

    # 14. cleanup_policies
    op.drop_index('ix_cleanup_policies_format',
                  table_name='cleanup_policies')
    op.drop_table('cleanup_policies')

    # 13. blobstore_configs
    op.drop_index('ix_blobstore_configs_type',
                  table_name='blobstore_configs')
    op.drop_table('blobstore_configs')

    # 12. audit_events
    op.drop_index('ix_audit_events_user_timestamp',
                  table_name='audit_events')
    op.drop_index('ix_audit_events_domain', table_name='audit_events')
    op.drop_index('ix_audit_events_timestamp', table_name='audit_events')
    op.drop_index('ix_audit_events_event_type', table_name='audit_events')
    op.drop_index('ix_audit_events_user_id', table_name='audit_events')
    op.drop_table('audit_events')

    # 11. system_configs
    op.drop_index('ix_system_configs_category',
                  table_name='system_configs')
    op.drop_table('system_configs')

    # 9. task_definitions
    op.drop_index('ix_task_definitions_enabled',
                  table_name='task_definitions')
    op.drop_index('ix_task_definitions_type',
                  table_name='task_definitions')
    op.drop_table('task_definitions')

    # 6. content_selectors
    op.drop_index('ix_content_selectors_type',
                  table_name='content_selectors')
    op.drop_table('content_selectors')

    # 5. privileges
    op.drop_index('ix_privileges_name', table_name='privileges')
    op.drop_index('ix_privileges_type', table_name='privileges')
    op.drop_table('privileges')

    # 3. roles
    op.drop_index('ix_roles_source', table_name='roles')
    op.drop_table('roles')

    # 2. users
    op.drop_index('ix_users_api_key', table_name='users')
    op.drop_index('ix_users_external_id', table_name='users')
    op.drop_index('ix_users_status', table_name='users')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')

    # 1. repositories
    op.drop_index('ix_repositories_online', table_name='repositories')
    op.drop_index('ix_repositories_format_type', table_name='repositories')
    op.drop_index('ix_repositories_type', table_name='repositories')
    op.drop_index('ix_repositories_format', table_name='repositories')
    op.drop_table('repositories')
