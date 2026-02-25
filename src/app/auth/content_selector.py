"""
Content Selector Expression Language (CSEL) Parser and Evaluator.

Replaces the CSEL expression evaluator from the Java source system
(``SecurityComponent`` CSEL evaluation within Apache Shiro 2.0.0).
Content Selectors enable **Tier 3** of the RBAC model — fine-grained
access control at the **asset level** within repositories.

CSEL expressions evaluate against asset properties (format, path,
coordinates) to determine if an asset matches the selector.  When
combined with ``'repository-content-selector'`` privileges they grant
access to specific subsets of repository content.

**Expression Language Reference:**

.. code-block:: text

    Operators
    =========
    ==   Equals (exact string match, case-sensitive)
    =^   Starts-with (prefix match)
    =~   Regex match (uses re.match — anchored at start)
    and  Logical AND (both operands must be True)
    or   Logical OR  (either operand may be True)
    not  Logical NOT (negation)

    Available Fields
    ================
    format                   Repository format ("maven2", "npm", "docker", …)
    path                     Asset path within the repository
    coordinate.groupId       Maven groupId (Maven format only)
    coordinate.artifactId    Maven artifactId (Maven format only)
    coordinate.version       Component version
    coordinate.extension     File extension

    Example Expressions
    ===================
    format == "maven2" and path =^ "/org/internal/"
    format == "npm" and path =^ "/@mycompany/"
    format == "docker" and path =~ ".*:latest"
    not (path =~ ".*-SNAPSHOT.*")
    (format == "maven2" or format == "npm") and path =^ "/com/example/"

Supports Feature **F-301** (Role-Based Access Control).

Compatibility:
    Python 3.12+, zero Java dependencies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.app.extensions import db
from src.app.models.content_selector import ContentSelector as ContentSelectorModel

# ---------------------------------------------------------------------------
# Module-Level Logger — replaces SLF4J 1.7.36
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "CSELNode",
    "ComparisonNode",
    "LogicalNode",
    "NotNode",
    "CSELTokenizer",
    "CSELParser",
    "ContentSelectorEvaluator",
    "CSELError",
    "CSELParseError",
    "CSELEvaluationError",
    "build_asset_context",
]


# ===========================================================================
# Exception Hierarchy
# ===========================================================================


class CSELError(Exception):
    """Base exception for all CSEL operations."""


class CSELParseError(CSELError):
    """Raised when a CSEL expression string cannot be parsed."""


class CSELEvaluationError(CSELError):
    """Raised when a parsed CSEL expression cannot be evaluated."""


# ===========================================================================
# AST Node Types (Abstract Syntax Tree)
# ===========================================================================


@dataclass
class CSELNode:
    """Base class for all CSEL expression AST nodes."""


@dataclass
class ComparisonNode(CSELNode):
    """Leaf node — represents ``field operator value``.

    Attributes:
        field:    Context field name (e.g. ``'format'``, ``'path'``).
        operator: Comparison operator (``'=='``, ``'=^'``, ``'=~'``).
        value:    Literal string to compare against.
    """

    field: str
    operator: str
    value: str


@dataclass
class LogicalNode(CSELNode):
    """Internal node — represents ``left operator right``.

    Attributes:
        operator: Logical keyword (``'and'`` or ``'or'``).
        left:     Left-hand operand subtree.
        right:    Right-hand operand subtree.
    """

    operator: str
    left: CSELNode
    right: CSELNode


@dataclass
class NotNode(CSELNode):
    """Unary node — represents ``not operand``.

    Attributes:
        operand: The subtree to negate.
    """

    operand: CSELNode


# ===========================================================================
# Token Types
# ===========================================================================

# Token kinds recognised by the tokenizer.
TOKEN_FIELD: str = "FIELD"
TOKEN_OPERATOR: str = "OPERATOR"
TOKEN_STRING: str = "STRING"
TOKEN_LOGICAL: str = "LOGICAL"
TOKEN_LPAREN: str = "LPAREN"
TOKEN_RPAREN: str = "RPAREN"
TOKEN_EOF: str = "EOF"


@dataclass
class Token:
    """A single lexical token produced by :class:`CSELTokenizer`.

    Attributes:
        kind:  One of the ``TOKEN_*`` constants.
        value: The raw string value of the token.
        pos:   Character position in the source expression (0-based).
    """

    kind: str
    value: str
    pos: int = 0


# ---------------------------------------------------------------------------
# Known comparison operators ordered longest-first so that ``==`` does not
# shadow ``=^`` or ``=~`` during tokenisation.
# ---------------------------------------------------------------------------
_COMPARISON_OPERATORS: Tuple[str, ...] = ("==", "=^", "=~")

# Logical keyword set (case-insensitive matching).
_LOGICAL_KEYWORDS: frozenset[str] = frozenset({"and", "or", "not"})

# Characters that are valid at the start of a CSEL field name.
_FIELD_START_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "_"
)

# Characters valid inside a CSEL field name (including '.' for nested coords).
_FIELD_BODY_CHARS: frozenset[str] = _FIELD_START_CHARS | frozenset(
    "0123456789."
)


# ===========================================================================
# CSELTokenizer
# ===========================================================================


class CSELTokenizer:
    """Lexer that splits a CSEL expression string into :class:`Token` objects.

    Usage::

        tokenizer = CSELTokenizer()
        tokens = tokenizer.tokenize('format == "maven2" and path =^ "/org/"')
    """

    def tokenize(self, expression: str) -> List[Token]:
        """Tokenize *expression* into a list of :class:`Token` instances.

        Args:
            expression: A CSEL expression string.

        Returns:
            Ordered list of tokens ending with a ``TOKEN_EOF`` sentinel.

        Raises:
            CSELParseError: If an unexpected character is encountered.
        """
        tokens: List[Token] = []
        pos: int = 0
        length: int = len(expression)

        while pos < length:
            ch: str = expression[pos]

            # Skip whitespace ------------------------------------------------
            if ch in (" ", "\t", "\n", "\r"):
                pos += 1
                continue

            # Parentheses ----------------------------------------------------
            if ch == "(":
                tokens.append(Token(TOKEN_LPAREN, "(", pos))
                pos += 1
                continue
            if ch == ")":
                tokens.append(Token(TOKEN_RPAREN, ")", pos))
                pos += 1
                continue

            # Comparison operators (==, =^, =~) ------------------------------
            if ch == "=" and pos + 1 < length:
                two_char: str = expression[pos : pos + 2]
                if two_char in _COMPARISON_OPERATORS:
                    tokens.append(Token(TOKEN_OPERATOR, two_char, pos))
                    pos += 2
                    continue
                # Standalone '=' is not a valid CSEL operator.
                raise CSELParseError(
                    f"Unexpected character '=' at position {pos}. "
                    f"Did you mean '==', '=^', or '=~'?"
                )

            # Quoted string literal ------------------------------------------
            if ch in ('"', "'"):
                token, end_pos = self._read_string(expression, pos)
                tokens.append(token)
                pos = end_pos
                continue

            # Field names / logical keywords ---------------------------------
            if ch in _FIELD_START_CHARS:
                token, end_pos = self._read_identifier(expression, pos)
                tokens.append(token)
                pos = end_pos
                continue

            # Unknown character ----------------------------------------------
            raise CSELParseError(
                f"Unexpected character {ch!r} at position {pos} "
                f"in expression: {expression!r}"
            )

        tokens.append(Token(TOKEN_EOF, "", pos))
        return tokens

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_string(expression: str, start: int) -> Tuple[Token, int]:
        """Read a quoted string literal starting at *start*.

        Supports both single (``'``) and double (``"``) quote delimiters.
        Backslash escapes (``\\"`` and ``\\\\``) are honoured.

        Returns:
            A ``(Token, end_position)`` tuple where *end_position* is the
            index immediately after the closing quote.
        """
        quote_char: str = expression[start]
        pos: int = start + 1
        length: int = len(expression)
        chars: list[str] = []

        while pos < length:
            ch: str = expression[pos]
            if ch == "\\":
                # Escape sequence — consume the next character literally.
                pos += 1
                if pos < length:
                    chars.append(expression[pos])
                    pos += 1
                else:
                    raise CSELParseError(
                        f"Unterminated escape sequence at position {pos - 1}"
                    )
            elif ch == quote_char:
                # Closing quote found.
                return Token(TOKEN_STRING, "".join(chars), start), pos + 1
            else:
                chars.append(ch)
                pos += 1

        raise CSELParseError(
            f"Unterminated string literal starting at position {start}"
        )

    @staticmethod
    def _read_identifier(expression: str, start: int) -> Tuple[Token, int]:
        """Read an identifier (field name or logical keyword) from *start*.

        Returns:
            A ``(Token, end_position)`` tuple.  The token kind is
            ``TOKEN_LOGICAL`` if the identifier is a keyword (``and``,
            ``or``, ``not``), otherwise ``TOKEN_FIELD``.
        """
        pos: int = start + 1
        length: int = len(expression)

        while pos < length and expression[pos] in _FIELD_BODY_CHARS:
            pos += 1

        word: str = expression[start:pos]
        if word.lower() in _LOGICAL_KEYWORDS:
            return Token(TOKEN_LOGICAL, word.lower(), start), pos
        return Token(TOKEN_FIELD, word, start), pos


# ===========================================================================
# CSELParser — Recursive-Descent Parser
# ===========================================================================


class CSELParser:
    """Parse a CSEL expression string into an AST (:class:`CSELNode` tree).

    Grammar (operator precedence lowest → highest)::

        expression     → or_expression
        or_expression  → and_expression ('or' and_expression)*
        and_expression → not_expression ('and' not_expression)*
        not_expression → 'not' not_expression | primary
        primary        → comparison | '(' expression ')'
        comparison     → FIELD OPERATOR STRING

    Usage::

        parser = CSELParser()
        ast = parser.parse('format == "maven2" and path =^ "/org/"')
    """

    def __init__(self) -> None:
        self._tokenizer: CSELTokenizer = CSELTokenizer()
        self._tokens: List[Token] = []
        self._pos: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, expression: str) -> CSELNode:
        """Parse *expression* and return the root :class:`CSELNode`.

        Args:
            expression: A CSEL expression string.

        Returns:
            The root node of the parsed AST.

        Raises:
            CSELParseError: On any syntax error.
        """
        if not expression or not expression.strip():
            raise CSELParseError("Empty CSEL expression")

        self._tokens = self._tokenizer.tokenize(expression)
        self._pos = 0

        node: CSELNode = self._parse_or_expression()

        # Ensure the entire token stream has been consumed.
        current: Token = self._current_token()
        if current.kind != TOKEN_EOF:
            raise CSELParseError(
                f"Unexpected token {current.value!r} at position "
                f"{current.pos} — expected end of expression"
            )
        return node

    # ------------------------------------------------------------------
    # Recursive-descent helpers
    # ------------------------------------------------------------------

    def _parse_or_expression(self) -> CSELNode:
        """``or_expression → and_expression ('or' and_expression)*``"""
        node: CSELNode = self._parse_and_expression()
        while self._match_logical("or"):
            right: CSELNode = self._parse_and_expression()
            node = LogicalNode(operator="or", left=node, right=right)
        return node

    def _parse_and_expression(self) -> CSELNode:
        """``and_expression → not_expression ('and' not_expression)*``"""
        node: CSELNode = self._parse_not_expression()
        while self._match_logical("and"):
            right: CSELNode = self._parse_not_expression()
            node = LogicalNode(operator="and", left=node, right=right)
        return node

    def _parse_not_expression(self) -> CSELNode:
        """``not_expression → 'not' not_expression | primary``"""
        if self._match_logical("not"):
            operand: CSELNode = self._parse_not_expression()
            return NotNode(operand=operand)
        return self._parse_primary()

    def _parse_primary(self) -> CSELNode:
        """``primary → comparison | '(' expression ')'``"""
        current: Token = self._current_token()

        # Parenthesised sub-expression ------------------------------------
        if current.kind == TOKEN_LPAREN:
            self._advance()  # consume '('
            node: CSELNode = self._parse_or_expression()
            closing: Token = self._current_token()
            if closing.kind != TOKEN_RPAREN:
                raise CSELParseError(
                    f"Expected ')' at position {closing.pos}, "
                    f"got {closing.value!r}"
                )
            self._advance()  # consume ')'
            return node

        # Comparison -------------------------------------------------------
        if current.kind == TOKEN_FIELD:
            return self._parse_comparison()

        raise CSELParseError(
            f"Unexpected token {current.value!r} ({current.kind}) "
            f"at position {current.pos} — expected field name or '('"
        )

    def _parse_comparison(self) -> ComparisonNode:
        """``comparison → FIELD OPERATOR STRING``"""
        field_token: Token = self._expect(TOKEN_FIELD)
        operator_token: Token = self._expect(TOKEN_OPERATOR)
        value_token: Token = self._expect(TOKEN_STRING)
        return ComparisonNode(
            field=field_token.value,
            operator=operator_token.value,
            value=value_token.value,
        )

    # ------------------------------------------------------------------
    # Token-stream utilities
    # ------------------------------------------------------------------

    def _current_token(self) -> Token:
        """Return the token at the current position without advancing."""
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        # Safety fallback — should never happen with EOF sentinel.
        return Token(TOKEN_EOF, "", -1)

    def _advance(self) -> Token:
        """Consume and return the current token, advancing position."""
        token: Token = self._current_token()
        self._pos += 1
        return token

    def _match_logical(self, keyword: str) -> bool:
        """If the current token is a LOGICAL token with *keyword*, consume it.

        Returns:
            ``True`` if consumed, ``False`` otherwise.
        """
        current: Token = self._current_token()
        if current.kind == TOKEN_LOGICAL and current.value == keyword:
            self._advance()
            return True
        return False

    def _expect(self, kind: str) -> Token:
        """Consume and return the current token if it matches *kind*.

        Raises:
            CSELParseError: If the current token has a different kind.
        """
        current: Token = self._current_token()
        if current.kind != kind:
            raise CSELParseError(
                f"Expected token kind {kind} at position {current.pos}, "
                f"got {current.kind} ({current.value!r})"
            )
        return self._advance()


# ===========================================================================
# ContentSelectorEvaluator
# ===========================================================================

# Maximum cached expression count before the LRU-style cache is evicted.
_MAX_CACHE_SIZE: int = 1024

# Regex compilation timeout safety: maximum pattern length to compile.
_MAX_REGEX_LENGTH: int = 4096


class ContentSelectorEvaluator:
    """Evaluate CSEL expressions against an asset property context.

    Combines parsing (via :class:`CSELParser`) with recursive AST
    evaluation.  Parsed ASTs are cached so that repeated evaluation of
    the same expression avoids re-parsing.

    This class also provides database-backed helpers that load
    :class:`~src.app.models.content_selector.ContentSelector` definitions
    by name and evaluate their stored expressions.

    Usage::

        evaluator = ContentSelectorEvaluator()
        result = evaluator.evaluate(
            'format == "maven2" and path =^ "/org/internal/"',
            {"format": "maven2", "path": "/org/internal/core-1.0.jar"},
        )
    """

    def __init__(self) -> None:
        self.parser: CSELParser = CSELParser()
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._cache: Dict[str, CSELNode] = {}

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(self, expression: str, context: Dict[str, Any]) -> bool:
        """Parse and evaluate a CSEL *expression* against *context*.

        Parsed ASTs are cached internally so repeated evaluations of the
        same expression string skip the parsing step.

        Args:
            expression: A CSEL expression string.
            context:    Dictionary mapping field names to string values.

        Returns:
            ``True`` if the expression matches the context.

        Raises:
            CSELParseError:      If the expression is syntactically invalid.
            CSELEvaluationError: If evaluation encounters an unexpected node.
        """
        if not expression or not expression.strip():
            self.logger.warning("Empty CSEL expression — returning False")
            return False

        ast_node: CSELNode = self._get_ast(expression)

        try:
            result: bool = self._evaluate_node(ast_node, context)
            self.logger.debug(
                "CSEL evaluation: expression=%r result=%s", expression, result
            )
            return result
        except CSELEvaluationError:
            raise
        except Exception as exc:
            self.logger.error(
                "CSEL evaluation failed: expression=%r error=%s",
                expression,
                exc,
            )
            raise CSELEvaluationError(
                f"Evaluation failed for expression {expression!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # AST cache
    # ------------------------------------------------------------------

    def _get_ast(self, expression: str) -> CSELNode:
        """Return the cached AST for *expression*, parsing if necessary."""
        cached: Optional[CSELNode] = self._cache.get(expression)
        if cached is not None:
            return cached

        # Evict oldest entries when cache exceeds the limit.
        if len(self._cache) >= _MAX_CACHE_SIZE:
            # Simple strategy: clear the entire cache.  A full LRU
            # implementation is not required for this use case.
            self.logger.debug(
                "CSEL expression cache full (%d entries) — clearing",
                len(self._cache),
            )
            self._cache.clear()

        node: CSELNode = self.parser.parse(expression)
        self._cache[expression] = node
        return node

    # ------------------------------------------------------------------
    # Recursive AST evaluation
    # ------------------------------------------------------------------

    def _evaluate_node(self, node: CSELNode, context: Dict[str, Any]) -> bool:
        """Recursively evaluate an AST *node* against *context*.

        Args:
            node:    An AST node (ComparisonNode, LogicalNode, or NotNode).
            context: Evaluation context dictionary.

        Returns:
            ``True`` if the sub-tree matches.

        Raises:
            CSELEvaluationError: On unrecognised node types.
        """
        if isinstance(node, ComparisonNode):
            return self._evaluate_comparison(
                node.field, node.operator, node.value, context
            )

        if isinstance(node, LogicalNode):
            if node.operator == "and":
                # Short-circuit: if left is False, skip right.
                return self._evaluate_node(
                    node.left, context
                ) and self._evaluate_node(node.right, context)
            if node.operator == "or":
                # Short-circuit: if left is True, skip right.
                return self._evaluate_node(
                    node.left, context
                ) or self._evaluate_node(node.right, context)
            raise CSELEvaluationError(
                f"Unknown logical operator: {node.operator!r}"
            )

        if isinstance(node, NotNode):
            return not self._evaluate_node(node.operand, context)

        raise CSELEvaluationError(
            f"Unknown AST node type: {type(node).__name__}"
        )

    def _evaluate_comparison(
        self,
        field: str,
        operator: str,
        value: str,
        context: Dict[str, Any],
    ) -> bool:
        """Evaluate a single comparison against *context*.

        If the *field* is absent from *context* the comparison returns
        ``False`` (no match) without raising an error.

        For the ``=~`` (regex) operator, ``re.match`` is used (anchored
        at the start of the string) for safety.  Extremely long patterns
        are rejected to prevent ReDoS.

        Args:
            field:    Context field name.
            operator: One of ``'=='``, ``'=^'``, ``'=~'``.
            value:    Literal comparison value.
            context:  Evaluation context dictionary.

        Returns:
            ``True`` if the comparison succeeds.
        """
        field_value: Any = context.get(field)
        if field_value is None:
            return False

        field_str: str = str(field_value)

        if operator == "==":
            return field_str == value

        if operator == "=^":
            return field_str.startswith(value)

        if operator == "=~":
            if len(value) > _MAX_REGEX_LENGTH:
                self.logger.warning(
                    "Regex pattern too long (%d chars) — rejecting for safety",
                    len(value),
                )
                return False
            try:
                return bool(re.match(value, field_str))
            except re.error as exc:
                self.logger.warning(
                    "Invalid regex pattern %r: %s", value, exc
                )
                return False

        self.logger.warning("Unknown comparison operator: %r", operator)
        return False

    # ------------------------------------------------------------------
    # Database-backed selector resolution
    # ------------------------------------------------------------------

    def get_selector_by_name(self, name: str) -> Optional[ContentSelectorModel]:
        """Load a :class:`ContentSelector` from the database by *name*.

        Args:
            name: The unique selector name.

        Returns:
            The :class:`ContentSelectorModel` instance, or ``None`` if
            no selector with the given name exists.
        """
        try:
            selector: Optional[ContentSelectorModel] = (
                ContentSelectorModel.query.filter_by(name=name).first()
            )
            if selector is None:
                self.logger.debug(
                    "Content selector not found: name=%r", name
                )
            return selector
        except Exception as exc:
            self.logger.error(
                "Failed to load content selector %r: %s", name, exc
            )
            return None

    def evaluate_selector(
        self, selector_name: str, context: Dict[str, Any]
    ) -> bool:
        """Load a selector by *selector_name* and evaluate its expression.

        Handles both CSEL (modern) and JEXL (legacy) expression types.
        JEXL expressions are treated as a best-effort evaluation: the
        module logs a warning and returns ``False`` since full JEXL
        interpretation is not implemented.

        Args:
            selector_name: The unique name of the content selector.
            context:       Evaluation context dictionary.

        Returns:
            ``True`` if the selector's expression matches the context.
            ``False`` if the selector is not found, has an unsupported
            type, or the expression does not match.
        """
        selector: Optional[ContentSelectorModel] = self.get_selector_by_name(
            selector_name
        )
        if selector is None:
            self.logger.warning(
                "Content selector %r not found — denying access",
                selector_name,
            )
            return False

        expression: str = selector.expression
        if not expression or not expression.strip():
            self.logger.warning(
                "Content selector %r has empty expression — denying access",
                selector_name,
            )
            return False

        # Check expression type.
        if selector.is_csel:
            return self.evaluate(expression, context)

        # Legacy JEXL support — log and deny.
        if selector.type == "jexl":
            self.logger.warning(
                "Content selector %r uses legacy JEXL expression type — "
                "JEXL evaluation is not implemented; denying access",
                selector_name,
            )
            return False

        self.logger.warning(
            "Content selector %r has unknown type %r — denying access",
            selector_name,
            selector.type,
        )
        return False

    def evaluate_selectors(
        self, selector_names: List[str], context: Dict[str, Any]
    ) -> bool:
        """Evaluate multiple selectors — **any** match grants access.

        Used when a user holds several ``'repository-content-selector'``
        privileges, each referencing a different content selector.

        Args:
            selector_names: List of content selector names to evaluate.
            context:        Evaluation context dictionary.

        Returns:
            ``True`` if **any** of the named selectors matches the
            context.
        """
        if not selector_names:
            return False

        for name in selector_names:
            try:
                if self.evaluate_selector(name, context):
                    self.logger.debug(
                        "Content selector %r matched — granting access", name
                    )
                    return True
            except CSELError as exc:
                self.logger.error(
                    "Error evaluating content selector %r: %s", name, exc
                )
                # Continue to the next selector rather than failing hard.
                continue

        self.logger.debug(
            "No content selectors matched out of %d — denying access",
            len(selector_names),
        )
        return False


# ===========================================================================
# Helper — build_asset_context
# ===========================================================================


def build_asset_context(asset: Any, repository: Any) -> Dict[str, Any]:
    """Build a CSEL evaluation context from an *asset* and *repository*.

    Maps asset and repository model properties to the CSEL field names
    expected by :class:`ContentSelectorEvaluator`.

    The *asset* object is expected to expose:
        - ``path`` (str): The asset's path within the repository.
        - ``attributes`` (dict | None): Format-specific metadata such as
          Maven coordinates.

    The *repository* object is expected to expose:
        - ``format`` (str): The repository format identifier.

    Args:
        asset:      An asset model instance (or any duck-typed equivalent).
        repository: A repository model instance (or duck-typed equivalent).

    Returns:
        A dictionary suitable for passing to
        :meth:`ContentSelectorEvaluator.evaluate`.
    """
    context: Dict[str, Any] = {
        "format": getattr(repository, "format", ""),
        "path": getattr(asset, "path", ""),
    }

    # Add format-specific coordinate fields from asset attributes.
    attrs: Dict[str, Any] = getattr(asset, "attributes", None) or {}

    _COORDINATE_FIELDS: Dict[str, str] = {
        "groupId": "coordinate.groupId",
        "artifactId": "coordinate.artifactId",
        "version": "coordinate.version",
        "extension": "coordinate.extension",
        "classifier": "coordinate.classifier",
        "packaging": "coordinate.packaging",
    }

    for attr_key, context_key in _COORDINATE_FIELDS.items():
        if attr_key in attrs:
            context[context_key] = attrs[attr_key]

    return context


logger.debug(
    "CSEL parser/evaluator module loaded — exports=%s",
    ", ".join(__all__),
)
