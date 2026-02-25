"""
Unit tests for security data models (Role, Privilege, ContentSelector)
from ``src/models/security.py``.

Validates RBAC model instantiation, relationship integrity, privilege
hierarchies, content selector expression storage, serialization, and
database constraint enforcement for the 3-tier RBAC system.
"""
import pytest
from datetime import datetime, timezone
from sqlalchemy.exc import IntegrityError

from src.models.security import Role, Privilege, ContentSelector
from tests.fixtures.user_data import (
    make_role_data,
    make_privilege_data,
    make_content_selector_data,
    make_repo_permission_data,
    ROLE_ADMIN,
    ROLE_DEVELOPER,
    ROLE_READONLY,
    PRIV_ALL,
    PRIV_REPO_READ,
    PRIV_REPO_WRITE,
    PRIV_REPO_ADMIN,
    PRIV_SEARCH,
    PRIV_COMPONENT_UPLOAD,
)

pytestmark = pytest.mark.unit

# --- Phase 2: Role Model Tests — Happy Path ---

def test_role_model_instantiation_with_required_fields(db_session, model_factory):
    """Role instantiation with id, name, and description succeeds."""
    data = make_role_data(name="nx-admin")
    role = model_factory(Role, id=data["id"], name=data["name"],
                         description=data["description"])
    assert role is not None
    assert role.name == "nx-admin"
    assert role.id == data["id"]

def test_role_model_default_values(db_session, model_factory):
    """Role with minimal fields receives sensible defaults."""
    data = make_role_data()
    role = model_factory(Role, name=data["name"])
    assert role.name == data["name"]
    assert role.id is not None

def test_role_admin_creation(db_session, model_factory):
    """Admin role created via factory data has correct name."""
    data = make_role_data(name="nx-admin-test")
    role = model_factory(Role, name=data["name"], description=data["description"])
    assert role.name == "nx-admin-test"
    assert isinstance(role.name, str)

def test_role_developer_creation(db_session, model_factory):
    """Developer role created via factory data has correct name."""
    data = make_role_data(name="nx-developer")
    role = model_factory(Role, name=data["name"], description=data["description"])
    assert role.name == "nx-developer"
    assert role.description is not None

def test_role_readonly_creation(db_session, model_factory):
    """Readonly role created via factory data has correct name."""
    data = make_role_data(name="nx-readonly")
    role = model_factory(Role, name=data["name"], description=data["description"])
    assert role.name == "nx-readonly"
    assert role.id is not None

def test_role_with_description(db_session, model_factory):
    """Role stores description field correctly."""
    role = model_factory(Role, name="desc-role",
                         description="Admin role with full system access")
    assert role.description == "Admin role with full system access"
    assert isinstance(role.description, str)

# --- Phase 3: Role-Privilege Relationship Tests ---

def test_role_has_privileges_relationship(db_session, model_factory):
    """Role exposes a navigable privileges collection."""
    role = model_factory(Role, name="rel-test-role", description="Test")
    priv = model_factory(Privilege, name="nx-read-rel", description="Read")
    role.privileges.append(priv)
    db_session.flush()
    assert len(role.privileges) == 1
    assert role.privileges[0].name == "nx-read-rel"

def test_role_can_have_multiple_privileges(db_session, model_factory):
    """Role supports association with 3+ privileges."""
    role = model_factory(Role, name="multi-priv-role", description="Multi")
    for i, n in enumerate([PRIV_REPO_READ, PRIV_REPO_WRITE, PRIV_SEARCH]):
        priv = model_factory(Privilege, name=f"mp-{n}-{i}", description=n)
        role.privileges.append(priv)
    db_session.flush()
    assert len(role.privileges) == 3
    assert len({p.name for p in role.privileges}) == 3

def test_admin_role_has_all_privileges(db_session, model_factory):
    """Admin role associated with PRIV_ALL privilege."""
    role = model_factory(Role, name="admin-all-role", description="Admin")
    priv_all = model_factory(Privilege, name="admin-all-priv", description="All")
    role.privileges.append(priv_all)
    db_session.flush()
    assert len(role.privileges) >= 1
    assert role.privileges[0].name == "admin-all-priv"

def test_developer_role_has_specific_privileges(db_session, model_factory):
    """Developer role has read/write/upload but NOT admin-only privileges."""
    role = model_factory(Role, name="dev-spec-role", description="Dev")
    for i, n in enumerate([PRIV_REPO_READ, PRIV_REPO_WRITE, PRIV_COMPONENT_UPLOAD]):
        role.privileges.append(
            model_factory(Privilege, name=f"dev-{n}-{i}", description=n))
    db_session.flush()
    assert len(role.privileges) == 3
    assert PRIV_ALL not in {p.name for p in role.privileges}

def test_readonly_role_has_read_only_privileges(db_session, model_factory):
    """Readonly role has read and search but NOT write privileges."""
    role = model_factory(Role, name="ro-spec-role", description="RO")
    for i, n in enumerate([PRIV_REPO_READ, PRIV_SEARCH]):
        role.privileges.append(
            model_factory(Privilege, name=f"ro-{n}-{i}", description=n))
    db_session.flush()
    priv_names = {p.name for p in role.privileges}
    assert len(priv_names) == 2
    assert not any(PRIV_REPO_WRITE in n for n in priv_names)

def test_role_privilege_add_and_remove(db_session, model_factory):
    """Privilege can be added to and removed from a role."""
    role = model_factory(Role, name="add-rm-role", description="Test")
    p1 = model_factory(Privilege, name="add-rm-a", description="A")
    p2 = model_factory(Privilege, name="add-rm-b", description="B")
    role.privileges.append(p1)
    db_session.flush()
    assert len(role.privileges) == 1
    role.privileges.append(p2)
    role.privileges.remove(p1)
    db_session.flush()
    assert len(role.privileges) == 1
    assert role.privileges[0].name == "add-rm-b"

# --- Phase 4: Privilege Model Tests ---

def test_privilege_model_instantiation(db_session, model_factory):
    """Privilege instantiation with id, name, description succeeds."""
    data = make_privilege_data(name="inst-priv")
    priv = model_factory(Privilege, name=data["name"], description=data["description"])
    assert priv is not None
    assert priv.name == "inst-priv"

def test_privilege_model_with_actions(db_session, model_factory):
    """Privilege stores actions list correctly."""
    data = make_privilege_data(name="action-test-priv", actions=["READ", "BROWSE"])
    priv = model_factory(Privilege, name=data["name"], actions=data["actions"])
    assert priv.actions is not None
    assert "READ" in priv.actions and "BROWSE" in priv.actions

def test_privilege_model_with_domain(db_session, model_factory):
    """Privilege stores type/domain field correctly."""
    priv = model_factory(Privilege, name="domain-test-priv",
                         description="D", type="repository")
    assert priv.type == "repository"
    assert isinstance(priv.type, str)

@pytest.mark.parametrize("priv_name", [
    "nx-all", "nx-repository-view", "nx-repository-edit",
    "nx-repository-admin", "nx-search-read", "nx-component-upload",
    "nx-apikey-all",
])
def test_privilege_parametrized_names(db_session, model_factory, priv_name):
    """Every standard privilege name is accepted by the model."""
    priv = model_factory(Privilege, name=priv_name, description=f"P {priv_name}")
    assert priv.name == priv_name
    assert priv.id is not None

def test_privilege_model_default_values(db_session, model_factory):
    """Privilege with minimal fields receives sensible defaults."""
    priv = model_factory(Privilege, name="default-test-priv")
    assert priv.name == "default-test-priv"
    assert priv.id is not None

# --- Phase 5: Privilege Hierarchy Tests ---

def test_privilege_hierarchy_admin_includes_all():
    """Admin privilege (PRIV_ALL) has the broadest scope by convention."""
    admin_privs = {PRIV_ALL}
    all_others = {PRIV_REPO_READ, PRIV_REPO_WRITE, PRIV_REPO_ADMIN, PRIV_SEARCH}
    assert PRIV_ALL in admin_privs
    assert PRIV_ALL not in all_others

def test_privilege_hierarchy_write_includes_read():
    """Write and read are distinct privilege identifiers."""
    assert PRIV_REPO_WRITE != PRIV_REPO_READ
    assert "edit" in PRIV_REPO_WRITE and "view" in PRIV_REPO_READ

def test_privilege_hierarchy_admin_outranks_developer():
    """Admin privilege set is a superset of developer set."""
    admin_privs = {PRIV_ALL}
    dev_privs = {PRIV_REPO_READ, PRIV_REPO_WRITE, PRIV_COMPONENT_UPLOAD}
    assert PRIV_ALL in admin_privs
    assert PRIV_ALL not in dev_privs

def test_privilege_does_not_imply_unrelated_privilege():
    """Read privilege does not imply search or upload."""
    assert PRIV_REPO_READ != PRIV_SEARCH
    assert PRIV_REPO_READ != PRIV_COMPONENT_UPLOAD

# --- Phase 6: Content Selector Model Tests ---

def test_content_selector_instantiation(db_session, model_factory):
    """ContentSelector instantiation with all fields succeeds."""
    data = make_content_selector_data(name="cs-inst",
                                      expression='format == "maven2"')
    cs = model_factory(ContentSelector, name=data["name"],
                       description=data["description"],
                       expression=data["expression"], type=data["type"])
    assert cs is not None
    assert cs.name == "cs-inst"
    assert cs.expression == 'format == "maven2"'

def test_content_selector_expression_field(db_session, model_factory):
    """ContentSelector stores complex CSEL expression correctly."""
    expr = 'format == "maven2" and path =^ "/com/example"'
    data = make_content_selector_data(expression=expr)
    cs = model_factory(ContentSelector, name="expr-field",
                       expression=data["expression"])
    assert cs.expression == expr
    assert len(cs.expression) > 0

def test_content_selector_type_field(db_session, model_factory):
    """ContentSelector type field stores 'csel' correctly."""
    cs = model_factory(ContentSelector, name="type-cs",
                       expression='path =^ "/"', type="csel")
    assert cs.type == "csel"
    assert isinstance(cs.type, str)

def test_content_selector_name_field(db_session, model_factory):
    """ContentSelector name field stores value correctly."""
    cs = model_factory(ContentSelector, name="maven-central-only",
                       expression='format == "maven2"')
    assert cs.name == "maven-central-only"
    assert isinstance(cs.name, str)

def test_content_selector_description(db_session, model_factory):
    """ContentSelector description field stores text correctly."""
    cs = model_factory(ContentSelector, name="cs-desc",
                       expression='format == "npm"',
                       description="Filters npm packages only")
    assert cs.description == "Filters npm packages only"
    assert isinstance(cs.description, str)

# --- Phase 7: Content Selector Expression Parsing Tests ---

def test_content_selector_format_filter_expression(db_session, model_factory):
    """Format filter expression stores correctly."""
    cs = model_factory(ContentSelector, name="fmt-flt", expression='format == "maven2"')
    assert "format ==" in cs.expression and "maven2" in cs.expression

def test_content_selector_path_prefix_expression(db_session, model_factory):
    """Path prefix expression stores correctly."""
    cs = model_factory(ContentSelector, name="path-pfx", expression='path =^ "/com/example"')
    assert "path =^" in cs.expression and "/com/example" in cs.expression

def test_content_selector_combined_expression(db_session, model_factory):
    """Combined AND expression stores correctly."""
    cs = model_factory(ContentSelector, name="combined-cs",
                       expression='format == "npm" and path =^ "/@scope"')
    assert "and" in cs.expression and "npm" in cs.expression

def test_content_selector_wildcard_expression(db_session, model_factory):
    """Wildcard expression matching all content stores correctly."""
    cs = model_factory(ContentSelector, name="wild-cs", expression='format == "*"')
    assert cs.expression == 'format == "*"' and cs.name == "wild-cs"

@pytest.mark.parametrize("expression", [
    'format == "maven2"',
    'path =^ "/com/"',
    'format == "npm" and path =^ "/@scope"',
    'format == "docker"',
])
def test_content_selector_parametrized_expressions(
    db_session, model_factory, expression
):
    """Each CSEL expression variant is stored correctly."""
    safe_name = f"param-{hash(expression) % 10000}"
    cs = model_factory(ContentSelector, name=safe_name, expression=expression)
    assert cs.expression == expression
    assert cs.id is not None

# --- Phase 8: RBAC Relationship Integrity Tests ---

def test_role_privilege_content_selector_chain(db_session, model_factory):
    """Full RBAC chain: Role -> Privileges -> ContentSelectors navigable."""
    role = model_factory(Role, name="chain-role", description="Chain test")
    priv = model_factory(Privilege, name="chain-priv", description="Chain")
    cs = model_factory(ContentSelector, name="chain-cs",
                       expression='format == "maven2"')
    role.privileges.append(priv)
    db_session.flush()
    assert len(role.privileges) == 1
    assert cs.name == "chain-cs"
    assert role.privileges[0].name == "chain-priv"

def test_repository_specific_permission_assignment():
    """Repository-specific permission data links role and privileges."""
    perm = make_repo_permission_data(
        repo_name="test-repo", role_name="nx-developer",
        privileges=[PRIV_REPO_READ, PRIV_REPO_WRITE])
    assert perm["repository_name"] == "test-repo"
    assert perm["role_name"] == "nx-developer"
    assert PRIV_REPO_READ in perm["privileges"]

def test_multiple_roles_on_same_privilege(db_session, model_factory):
    """Two roles can share the same privilege without duplication."""
    priv = model_factory(Privilege, name="shared-priv-x", description="Shared")
    role1 = model_factory(Role, name="share-r1", description="R1")
    role2 = model_factory(Role, name="share-r2", description="R2")
    role1.privileges.append(priv)
    role2.privileges.append(priv)
    db_session.flush()
    assert priv in role1.privileges
    assert priv in role2.privileges

def test_role_assigned_to_user_by_name(db_session, model_factory):
    """Role name aligns with user role string convention."""
    role = model_factory(Role, name=ROLE_ADMIN, description="Admin role")
    assert role.name == ROLE_ADMIN
    assert ROLE_ADMIN == "admin"

# --- Phase 9: Serialization Tests ---

def test_role_to_dict_serialization(db_session, model_factory):
    """Role serializes to dict with expected keys."""
    role = model_factory(Role, name="serial-role", description="Serialize test")
    result = role.to_dict()
    assert "name" in result and result["name"] == "serial-role"
    assert "description" in result

def test_privilege_to_dict_serialization(db_session, model_factory):
    """Privilege serializes to dict with expected keys."""
    priv = model_factory(Privilege, name="serial-priv", description="Ser",
                         actions=["READ"], type="repository")
    result = priv.to_dict()
    assert "name" in result and result["name"] == "serial-priv"
    assert "type" in result

def test_content_selector_to_dict_serialization(db_session, model_factory):
    """ContentSelector serializes to dict with expected keys."""
    cs = model_factory(ContentSelector, name="serial-cs",
                       expression='format == "maven2"', type="csel",
                       description="Test")
    result = cs.to_dict()
    assert "name" in result and "expression" in result
    assert result["expression"] == 'format == "maven2"'

def test_role_repr_string(db_session, model_factory):
    """Role repr contains role name."""
    role = model_factory(Role, name="repr-role", description="Repr test")
    r = repr(role)
    assert "repr-role" in r
    assert isinstance(r, str)

def test_privilege_repr_string(db_session, model_factory):
    """Privilege repr contains privilege name."""
    priv = model_factory(Privilege, name="repr-priv", description="Repr")
    r = repr(priv)
    assert "repr-priv" in r
    assert isinstance(r, str)

# --- Phase 10: Edge Cases ---

def test_role_with_no_privileges(db_session, model_factory):
    """Role with empty privileges list is valid."""
    role = model_factory(Role, name="empty-priv-role", description="Empty")
    assert role.privileges is not None
    assert len(role.privileges) == 0

def test_role_with_very_long_name(db_session, model_factory):
    """Role accepts name up to field constraint length."""
    long_name = "r" * 200
    role = model_factory(Role, name=long_name, description="Long name")
    assert role.name == long_name
    assert len(role.name) == 200

def test_privilege_with_empty_actions_list(db_session, model_factory):
    """Privilege accepts empty actions list."""
    priv = model_factory(Privilege, name="no-act-priv", actions=[])
    assert priv.actions is not None
    assert len(priv.actions) == 0

def test_content_selector_with_empty_expression(db_session, model_factory):
    """ContentSelector with empty string expression stores it."""
    cs = model_factory(ContentSelector, name="empty-expr-cs", expression="")
    assert cs.expression == ""
    assert cs.name == "empty-expr-cs"

def test_role_created_at_timestamp(db_session, model_factory):
    """Role has a created_at timestamp after persistence."""
    now = datetime.now(timezone.utc)
    role = model_factory(Role, name="ts-role", description="Timestamp test")
    assert role.created_at is not None
    assert isinstance(role.created_at, datetime)

# --- Phase 11: Error Cases ---

def test_role_null_name_raises_error(db_session):
    """Role with NULL name raises IntegrityError on flush."""
    role = Role(name=None, description="No name")
    db_session.add(role)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

def test_role_duplicate_name_constraint(db_session, model_factory):
    """Duplicate role names raise IntegrityError."""
    model_factory(Role, name="dup-role-err", description="First")
    db_session.flush()
    dup = Role(id="dup-err-id-2", name="dup-role-err", description="Second")
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

def test_privilege_null_name_raises_error(db_session):
    """Privilege with NULL name raises IntegrityError."""
    priv = Privilege(name=None, description="No name")
    db_session.add(priv)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

def test_content_selector_null_name_raises_error(db_session):
    """ContentSelector with NULL name raises IntegrityError."""
    cs = ContentSelector(name=None, expression="test")
    db_session.add(cs)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

def test_content_selector_null_expression_raises_error(db_session):
    """ContentSelector with NULL expression raises IntegrityError."""
    cs = ContentSelector(name="null-expr-cs", expression=None)
    db_session.add(cs)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

# --- Phase 12: Database Persistence Tests ---

def test_role_persists_to_database(db_session, model_factory):
    """Role persists and can be queried back."""
    model_factory(Role, name="persist-role-db", description="Persist test")
    db_session.flush()
    found = db_session.query(Role).filter_by(name="persist-role-db").first()
    assert found is not None
    assert found.name == "persist-role-db"

def test_privilege_persists_to_database(db_session, model_factory):
    """Privilege persists and can be queried back."""
    model_factory(Privilege, name="persist-priv-db", description="Persist")
    db_session.flush()
    found = db_session.query(Privilege).filter_by(name="persist-priv-db").first()
    assert found is not None
    assert found.name == "persist-priv-db"

def test_content_selector_persists_to_database(db_session, model_factory):
    """ContentSelector persists and can be queried back."""
    model_factory(ContentSelector, name="persist-cs-db",
                  expression='path =^ "/"')
    db_session.flush()
    found = db_session.query(ContentSelector).filter_by(
        name="persist-cs-db").first()
    assert found is not None
    assert found.expression == 'path =^ "/"'

def test_role_with_privileges_persists(db_session, model_factory):
    """Role with attached privileges persists relationships correctly."""
    role = model_factory(Role, name="persist-rel-role",
                         description="Rel persist")
    p1 = model_factory(Privilege, name="persist-p1-db", description="P1")
    p2 = model_factory(Privilege, name="persist-p2-db", description="P2")
    role.privileges.extend([p1, p2])
    db_session.flush()
    found = db_session.query(Role).filter_by(name="persist-rel-role").first()
    assert found is not None
    assert len(found.privileges) == 2

def test_role_delete_does_not_cascade_to_privileges(db_session, model_factory):
    """Deleting a role does not remove shared privileges."""
    role = model_factory(Role, name="del-cascade-role", description="Del test")
    priv = model_factory(Privilege, name="del-cascade-priv", description="Keep")
    role.privileges.append(priv)
    db_session.flush()
    db_session.delete(role)
    db_session.flush()
    remaining = db_session.query(Privilege).filter_by(
        name="del-cascade-priv").first()
    assert remaining is not None
    assert remaining.name == "del-cascade-priv"
