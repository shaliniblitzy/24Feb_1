"""Initial database schema — create all tables.

Creates 16 tables across 6 data domains:
- Repository domain: repositories, proxy_configs, group_configs
- Component domain: components, assets
- Security domain: users, roles, privileges, content_selectors, role_assignments
- Administration domain: task_definitions, task_executions, system_configs
- Audit domain: audit_events
- Storage domain: blobstore_configs, cleanup_policies

Revision ID: 001_initial_schema
Revises: (none — initial migration)
Create Date: 2026-02-24

Replaces Flyway 8.5.13 initial schema migration from the original Java stack.
Supports both SQLite (standalone) and PostgreSQL (clustered/production) databases.
All primary keys use CHAR(36) UUID strings for cross-database compatibility.
"""

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic revision metadata
# ---------------------------------------------------------------------------
revision = '001_initial_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all 16 database tables in dependency order.

    Tables are created in the following order to respect foreign key
    constraints (parent tables before child tables):

    1. Independent parent tables:
       - repositories, users, roles, privileges, content_selectors,
         task_definitions, system_configs, blobstore_configs, cleanup_policies,
         audit_events
    2. Child tables with foreign key dependencies:
       - proxy_configs (-> repositories)
       - group_configs (-> repositories)
       - components (-> repositories)
       - assets (-> components)
       - role_assignments (-> users, roles)
       - task_executions (-> task_definitions)

    All tables include TimestampMixin columns (created_at, updated_at).
    Tables with SoftDeleteMixin also include deleted_at and is_deleted columns.
    """

    # -----------------------------------------------------------------------
    # 1. REPOSITORY DOMAIN — Parent table: repositories
    # -----------------------------------------------------------------------
    op.create_table(
        'repositories',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('format', sa.String(64), nullable=False),
        sa.Column(
            'type',
            sa.Enum('hosted', 'proxy', 'group', name='repository_type_enum'),
            nullable=False,
        ),
        sa.Column(
            'status',
            sa.Enum(
                'NEW', 'INITIALIZING', 'STARTED', 'STOPPED', 'DELETED', 'FAILED',
                name='repository_status_enum',
            ),
            nullable=False,
            server_default='NEW',
        ),
        sa.Column('online', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column(
            'blobstore_name', sa.String(256), nullable=True, server_default='default',
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_repositories_name'),
    )
    op.create_index('ix_repository_format', 'repositories', ['format'])
    op.create_index('ix_repository_type', 'repositories', ['type'])
    op.create_index('ix_repository_status', 'repositories', ['status'])
    op.create_index(
        'ix_repository_format_type', 'repositories', ['format', 'type'],
    )
    op.create_index('ix_repositories_is_deleted', 'repositories', ['is_deleted'])

    # -----------------------------------------------------------------------
    # 2. REPOSITORY DOMAIN — Child table: proxy_configs
    # -----------------------------------------------------------------------
    op.create_table(
        'proxy_configs',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column(
            'repository_id',
            sa.CHAR(36),
            sa.ForeignKey('repositories.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('remote_url', sa.String(2048), nullable=False),
        sa.Column(
            'content_max_age', sa.Integer(), nullable=False, server_default='1440',
        ),
        sa.Column(
            'metadata_max_age', sa.Integer(), nullable=False, server_default='1440',
        ),
        sa.Column(
            'negative_cache_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('1'),
        ),
        sa.Column(
            'negative_cache_ttl', sa.Integer(), nullable=False, server_default='1440',
        ),
        sa.Column('remote_auth_type', sa.String(64), nullable=True),
        sa.Column('remote_auth_credentials', sa.JSON(), nullable=True),
        sa.Column(
            'blocked', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.Column(
            'auto_block', sa.Boolean(), nullable=False, server_default=sa.text('1'),
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repository_id', name='uq_proxy_configs_repository_id'),
    )

    # -----------------------------------------------------------------------
    # 3. REPOSITORY DOMAIN — Child table: group_configs
    # -----------------------------------------------------------------------
    op.create_table(
        'group_configs',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column(
            'repository_id',
            sa.CHAR(36),
            sa.ForeignKey('repositories.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('member_names', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repository_id', name='uq_group_configs_repository_id'),
    )

    # -----------------------------------------------------------------------
    # 4. COMPONENT DOMAIN — Parent table: components
    # -----------------------------------------------------------------------
    op.create_table(
        'components',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column(
            'repository_id',
            sa.CHAR(36),
            sa.ForeignKey('repositories.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('namespace', sa.String(512), nullable=True),
        sa.Column('name', sa.String(512), nullable=False),
        sa.Column('version', sa.String(256), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'repository_id', 'namespace', 'name', 'version',
            name='uq_component_coordinates',
        ),
    )
    op.create_index(
        'ix_component_repo_name', 'components', ['repository_id', 'name'],
    )
    op.create_index('ix_component_namespace', 'components', ['namespace'])
    op.create_index('ix_components_is_deleted', 'components', ['is_deleted'])

    # -----------------------------------------------------------------------
    # 5. COMPONENT DOMAIN — Child table: assets
    # -----------------------------------------------------------------------
    op.create_table(
        'assets',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column(
            'component_id',
            sa.CHAR(36),
            sa.ForeignKey('components.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('path', sa.String(2048), nullable=False),
        sa.Column('content_type', sa.String(256), nullable=True),
        sa.Column('size', sa.BigInteger(), nullable=True),
        sa.Column('blob_ref', sa.String(512), nullable=True),
        sa.Column('checksums', sa.JSON(), nullable=True),
        sa.Column('last_downloaded', sa.DateTime(), nullable=True),
        sa.Column(
            'download_count', sa.BigInteger(), nullable=True, server_default='0',
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_asset_path', 'assets', ['path'])
    op.create_index('ix_asset_blob_ref', 'assets', ['blob_ref'])
    op.create_index(
        'ix_asset_component_path', 'assets', ['component_id', 'path'],
    )
    op.create_index('ix_assets_is_deleted', 'assets', ['is_deleted'])

    # -----------------------------------------------------------------------
    # 6. SECURITY DOMAIN — Parent table: users
    # -----------------------------------------------------------------------
    op.create_table(
        'users',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('username', sa.String(256), nullable=False),
        sa.Column('password_hash', sa.String(512), nullable=True),
        sa.Column('email', sa.String(512), nullable=True),
        sa.Column('first_name', sa.String(256), nullable=True),
        sa.Column('last_name', sa.String(256), nullable=True),
        sa.Column(
            'status',
            sa.Enum('active', 'disabled', 'locked', name='user_status_enum'),
            nullable=False,
            server_default='active',
        ),
        sa.Column('source', sa.String(64), nullable=True, server_default='local'),
        sa.Column('api_key', sa.String(512), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username', name='uq_users_username'),
        sa.UniqueConstraint('api_key', name='uq_users_api_key'),
    )
    op.create_index('ix_user_email', 'users', ['email'])
    op.create_index('ix_user_status', 'users', ['status'])
    op.create_index('ix_user_api_key', 'users', ['api_key'])
    op.create_index('ix_users_is_deleted', 'users', ['is_deleted'])

    # -----------------------------------------------------------------------
    # 7. SECURITY DOMAIN — Parent table: roles
    # -----------------------------------------------------------------------
    op.create_table(
        'roles',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('privileges', sa.JSON(), nullable=True),
        sa.Column('source', sa.String(64), nullable=True, server_default='local'),
        sa.Column(
            'read_only', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_roles_name'),
    )
    op.create_index('ix_roles_is_deleted', 'roles', ['is_deleted'])

    # -----------------------------------------------------------------------
    # 8. SECURITY DOMAIN — Parent table: privileges
    # -----------------------------------------------------------------------
    op.create_table(
        'privileges',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('type', sa.String(64), nullable=False),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('properties', sa.JSON(), nullable=True),
        sa.Column(
            'read_only', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_privileges_name'),
    )
    op.create_index('ix_privilege_type', 'privileges', ['type'])

    # -----------------------------------------------------------------------
    # 9. SECURITY DOMAIN — Parent table: content_selectors
    # -----------------------------------------------------------------------
    op.create_table(
        'content_selectors',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('type', sa.String(64), nullable=False, server_default='csel'),
        sa.Column('expression', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_content_selectors_name'),
    )

    # -----------------------------------------------------------------------
    # 10. SECURITY DOMAIN — Association table: role_assignments (composite PK)
    # -----------------------------------------------------------------------
    op.create_table(
        'role_assignments',
        sa.Column(
            'user_id',
            sa.CHAR(36),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'role_id',
            sa.CHAR(36),
            sa.ForeignKey('roles.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'role_id'),
    )

    # -----------------------------------------------------------------------
    # 11. ADMINISTRATION DOMAIN — Parent table: task_definitions
    # -----------------------------------------------------------------------
    op.create_table(
        'task_definitions',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('type', sa.String(256), nullable=False),
        sa.Column('name', sa.String(512), nullable=False),
        sa.Column('schedule_cron', sa.String(256), nullable=True),
        sa.Column(
            'enabled', sa.Boolean(), nullable=False, server_default=sa.text('1'),
        ),
        sa.Column('properties', sa.JSON(), nullable=True),
        sa.Column('alert_email', sa.String(512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_def_type', 'task_definitions', ['type'])
    op.create_index('ix_task_def_enabled', 'task_definitions', ['enabled'])
    op.create_index(
        'ix_task_definitions_is_deleted', 'task_definitions', ['is_deleted'],
    )

    # -----------------------------------------------------------------------
    # 12. ADMINISTRATION DOMAIN — Child table: task_executions
    # -----------------------------------------------------------------------
    op.create_table(
        'task_executions',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column(
            'task_id',
            sa.CHAR(36),
            sa.ForeignKey('task_definitions.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('start_time', sa.DateTime(), nullable=False),
        sa.Column('end_time', sa.DateTime(), nullable=True),
        sa.Column(
            'status',
            sa.Enum(
                'running', 'completed', 'failed', 'cancelled',
                name='task_execution_status_enum',
            ),
            nullable=False,
            server_default='running',
        ),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('trigger_type', sa.String(64), nullable=True),
        sa.Column('node_id', sa.String(256), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_exec_start_time', 'task_executions', ['start_time'])
    op.create_index('ix_task_exec_status', 'task_executions', ['status'])
    op.create_index(
        'ix_task_exec_task_status', 'task_executions', ['task_id', 'status'],
    )

    # -----------------------------------------------------------------------
    # 13. ADMINISTRATION DOMAIN — Parent table: system_configs
    # -----------------------------------------------------------------------
    op.create_table(
        'system_configs',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('key', sa.String(512), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('type', sa.String(64), nullable=True, server_default='string'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key', name='uq_system_configs_key'),
    )

    # -----------------------------------------------------------------------
    # 14. AUDIT DOMAIN — audit_events
    # -----------------------------------------------------------------------
    op.create_table(
        'audit_events',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('event_type', sa.String(128), nullable=False),
        sa.Column('user_id', sa.String(256), nullable=True),
        sa.Column('source_ip', sa.String(45), nullable=True),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_event_timestamp', 'audit_events', ['timestamp'])
    op.create_index('ix_audit_event_event_type', 'audit_events', ['event_type'])
    op.create_index(
        'ix_audit_event_timestamp_type',
        'audit_events',
        ['timestamp', 'event_type'],
    )
    op.create_index('ix_audit_event_user', 'audit_events', ['user_id'])

    # -----------------------------------------------------------------------
    # 15. STORAGE DOMAIN — Parent table: blobstore_configs
    # -----------------------------------------------------------------------
    op.create_table(
        'blobstore_configs',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column(
            'type',
            sa.Enum('file', 's3', name='blobstore_type_enum'),
            nullable=False,
        ),
        sa.Column('attributes', sa.JSON(), nullable=True),
        sa.Column('state', sa.String(64), nullable=True, server_default='ACTIVE'),
        sa.Column('blob_count', sa.BigInteger(), nullable=True, server_default='0'),
        sa.Column('total_size', sa.BigInteger(), nullable=True, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_blobstore_configs_name'),
    )
    op.create_index(
        'ix_blobstore_configs_is_deleted', 'blobstore_configs', ['is_deleted'],
    )

    # -----------------------------------------------------------------------
    # 16. STORAGE DOMAIN — Parent table: cleanup_policies
    # -----------------------------------------------------------------------
    op.create_table(
        'cleanup_policies',
        sa.Column('id', sa.CHAR(36), nullable=False),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('format', sa.String(64), nullable=True),
        sa.Column('criteria', sa.JSON(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column(
            'is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_cleanup_policies_name'),
    )
    op.create_index(
        'ix_cleanup_policies_is_deleted', 'cleanup_policies', ['is_deleted'],
    )


def downgrade() -> None:
    """Drop all 16 database tables in reverse dependency order.

    Child tables with foreign keys are dropped before their parent tables
    to avoid referential integrity violations. After all tables are dropped,
    PostgreSQL enum types are cleaned up (SQLite does not use real enum types).
    """

    # -----------------------------------------------------------------------
    # Drop indexes first, then tables in reverse dependency order
    # Child tables (with foreign keys) are dropped before parent tables.
    # -----------------------------------------------------------------------

    # 1. Drop cleanup_policies (independent — storage domain)
    op.drop_index('ix_cleanup_policies_is_deleted', table_name='cleanup_policies')
    op.drop_table('cleanup_policies')

    # 2. Drop blobstore_configs (independent — storage domain)
    op.drop_index('ix_blobstore_configs_is_deleted', table_name='blobstore_configs')
    op.drop_table('blobstore_configs')

    # 3. Drop audit_events (independent — audit domain)
    op.drop_index('ix_audit_event_user', table_name='audit_events')
    op.drop_index('ix_audit_event_timestamp_type', table_name='audit_events')
    op.drop_index('ix_audit_event_event_type', table_name='audit_events')
    op.drop_index('ix_audit_event_timestamp', table_name='audit_events')
    op.drop_table('audit_events')

    # 4. Drop system_configs (independent — administration domain)
    op.drop_table('system_configs')

    # 5. Drop task_executions (child of task_definitions — administration domain)
    op.drop_index('ix_task_exec_task_status', table_name='task_executions')
    op.drop_index('ix_task_exec_status', table_name='task_executions')
    op.drop_index('ix_task_exec_start_time', table_name='task_executions')
    op.drop_table('task_executions')

    # 6. Drop task_definitions (independent after task_executions — admin domain)
    op.drop_index('ix_task_definitions_is_deleted', table_name='task_definitions')
    op.drop_index('ix_task_def_enabled', table_name='task_definitions')
    op.drop_index('ix_task_def_type', table_name='task_definitions')
    op.drop_table('task_definitions')

    # 7. Drop role_assignments (child of users and roles — security domain)
    op.drop_table('role_assignments')

    # 8. Drop content_selectors (independent — security domain)
    op.drop_table('content_selectors')

    # 9. Drop privileges (independent — security domain)
    op.drop_index('ix_privilege_type', table_name='privileges')
    op.drop_table('privileges')

    # 10. Drop roles (independent after role_assignments — security domain)
    op.drop_index('ix_roles_is_deleted', table_name='roles')
    op.drop_table('roles')

    # 11. Drop users (independent after role_assignments — security domain)
    op.drop_index('ix_users_is_deleted', table_name='users')
    op.drop_index('ix_user_api_key', table_name='users')
    op.drop_index('ix_user_status', table_name='users')
    op.drop_index('ix_user_email', table_name='users')
    op.drop_table('users')

    # 12. Drop assets (child of components — component domain)
    op.drop_index('ix_assets_is_deleted', table_name='assets')
    op.drop_index('ix_asset_component_path', table_name='assets')
    op.drop_index('ix_asset_blob_ref', table_name='assets')
    op.drop_index('ix_asset_path', table_name='assets')
    op.drop_table('assets')

    # 13. Drop components (child of repositories — component domain)
    op.drop_index('ix_components_is_deleted', table_name='components')
    op.drop_index('ix_component_namespace', table_name='components')
    op.drop_index('ix_component_repo_name', table_name='components')
    op.drop_table('components')

    # 14. Drop group_configs (child of repositories — repository domain)
    op.drop_table('group_configs')

    # 15. Drop proxy_configs (child of repositories — repository domain)
    op.drop_table('proxy_configs')

    # 16. Drop repositories (independent after children — repository domain)
    op.drop_index('ix_repositories_is_deleted', table_name='repositories')
    op.drop_index('ix_repository_format_type', table_name='repositories')
    op.drop_index('ix_repository_status', table_name='repositories')
    op.drop_index('ix_repository_type', table_name='repositories')
    op.drop_index('ix_repository_format', table_name='repositories')
    op.drop_table('repositories')

    # -----------------------------------------------------------------------
    # Drop PostgreSQL enum types (no-op on SQLite via checkfirst=True)
    # -----------------------------------------------------------------------
    bind = op.get_bind()
    sa.Enum(name='task_execution_status_enum').drop(bind, checkfirst=True)
    sa.Enum(name='blobstore_type_enum').drop(bind, checkfirst=True)
    sa.Enum(name='user_status_enum').drop(bind, checkfirst=True)
    sa.Enum(name='repository_status_enum').drop(bind, checkfirst=True)
    sa.Enum(name='repository_type_enum').drop(bind, checkfirst=True)
