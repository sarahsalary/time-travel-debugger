"""TTDQL: a safe, compact query language for structured log events.

Examples::

    level=ERROR AND duration_ms >= 500
    service IN (api, gateway) AND extra.http.status >= 500
    message ILIKE '%timeout%'
    contains(lower(message), 'fail') AND hour(timestamp) = 14
    age(timestamp) < 3600

Function calls are evaluated against event values. No user-supplied Python
code is executed and there is no network/file access in the evaluator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable

from .models import LogEvent


class QuerySyntaxError(ValueError):
    def __init__(self, message: str, position: int = 0) -> None:
        self.position = position
        super().__init__(f"{message} (at position {position})")


class TokenKind(Enum):
    IDENT = auto()
    STRING = auto()
    NUMBER = auto()
    BOOL = auto()
    NULL = auto()
    OPERATOR = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    IN = auto()
    LIKE = auto()
    ILIKE = auto()
    EOF = auto()


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: Any
    position: int


_KEYWORDS: dict[str, TokenKind] = {
    "AND": TokenKind.AND, "OR": TokenKind.OR, "NOT": TokenKind.NOT,
    "IN": TokenKind.IN, "LIKE": TokenKind.LIKE, "ILIKE": TokenKind.ILIKE,
    "TRUE": TokenKind.BOOL, "FALSE": TokenKind.BOOL, "NULL": TokenKind.NULL,
}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)(?:[Ee][+-]?\d+)?")
_OPERATORS = ("!=", ">=", "<=", "!~", "=", ">", "<", "~")


class Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.position = 0

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.position < len(self.text):
            start = self.position
            char = self.text[start]
            if char.isspace():
                self.position += 1
                continue
            specials = {"(": TokenKind.LPAREN, ")": TokenKind.RPAREN, ",": TokenKind.COMMA}
            if char in specials:
                tokens.append(Token(specials[char], char, start))
                self.position += 1
                continue
            if char in ('"', "'"):
                tokens.append(self._string())
                continue
            op = next((item for item in _OPERATORS if self.text.startswith(item, start)), None)
            if op is not None:
                tokens.append(Token(TokenKind.OPERATOR, op, start))
                self.position += len(op)
                continue
            numeric = _NUMBER.match(self.text, start)
            if numeric and (numeric.end() == len(self.text) or not self.text[numeric.end()].isalnum()):
                token = numeric.group()
                tokens.append(Token(TokenKind.NUMBER, float(token) if any(c in token for c in '.Ee') else int(token), start))
                self.position = numeric.end()
                continue
            identifier = _IDENT.match(self.text, start)
            if identifier:
                name = identifier.group()
                kind = _KEYWORDS.get(name.upper(), TokenKind.IDENT)
                value = name.upper() if kind is not TokenKind.IDENT else name
                if kind is TokenKind.BOOL:
                    value = name.upper() == "TRUE"
                if kind is TokenKind.NULL:
                    value = None
                tokens.append(Token(kind, value, start))
                self.position = identifier.end()
                continue
            raise QuerySyntaxError(f"Unexpected character {char!r}", start)
        tokens.append(Token(TokenKind.EOF, None, self.position))
        return tokens

    def _string(self) -> Token:
        start = self.position
        delimiter = self.text[self.position]
        self.position += 1
        result: list[str] = []
        escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"'}
        while self.position < len(self.text):
            char = self.text[self.position]
            self.position += 1
            if char == delimiter:
                return Token(TokenKind.STRING, "".join(result), start)
            if char == "\\" and self.position < len(self.text):
                char = escapes.get(self.text[self.position], self.text[self.position])
                self.position += 1
            result.append(char)
        raise QuerySyntaxError("Unterminated string literal", start)


@dataclass(frozen=True)
class Node:
    pass


@dataclass(frozen=True)
class Literal(Node):
    value: Any


@dataclass(frozen=True)
class FieldRef(Node):
    name: str


@dataclass(frozen=True)
class FunctionCall(Node):
    name: str
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Comparison(Node):
    left: Node
    operator: str
    right: Node


@dataclass(frozen=True)
class Membership(Node):
    left: Node
    values: tuple[Node, ...]
    negated: bool = False


@dataclass(frozen=True)
class Like(Node):
    left: Node
    pattern: Node
    case_sensitive: bool = True


@dataclass(frozen=True)
class BinaryOp(Node):
    left: Node
    op: str
    right: Node


@dataclass(frozen=True)
class NotOp(Node):
    operand: Node


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.position = 0

    def _peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.position + offset, len(self.tokens) - 1)]

    def _advance(self) -> Token:
        result = self._peek()
        self.position += 1
        return result

    def _expect(self, kind: TokenKind, message: str) -> Token:
        candidate = self._peek()
        if candidate.kind is not kind:
            raise QuerySyntaxError(f"Expected {message}, got {candidate.kind.name}", candidate.position)
        return self._advance()

    def parse(self) -> Node:
        node = self._or()
        self._expect(TokenKind.EOF, "end of query")
        return node

    def _or(self) -> Node:
        result = self._and()
        while self._peek().kind is TokenKind.OR:
            self._advance()
            result = BinaryOp(result, "OR", self._and())
        return result

    def _and(self) -> Node:
        result = self._not()
        while self._peek().kind is TokenKind.AND:
            self._advance()
            result = BinaryOp(result, "AND", self._not())
        return result

    def _not(self) -> Node:
        if self._peek().kind is TokenKind.NOT:
            self._advance()
            return NotOp(self._not())
        if self._peek().kind is TokenKind.LPAREN:
            self._advance()
            result = self._or()
            self._expect(TokenKind.RPAREN, "closing parenthesis")
            return result
        return self._comparison()

    def _comparison(self) -> Node:
        left = self._value(identifier_as_field=True)
        kind = self._peek().kind
        negated = False
        if kind is TokenKind.NOT and self._peek(1).kind is TokenKind.IN:
            self._advance()
            kind = TokenKind.IN
            negated = True
        if kind is TokenKind.IN:
            self._advance()
            self._expect(TokenKind.LPAREN, "opening IN parenthesis")
            values = []
            if self._peek().kind is TokenKind.RPAREN:
                raise QuerySyntaxError("IN list must not be empty", self._peek().position)
            while True:
                values.append(self._value(identifier_as_field=False))
                if self._peek().kind is not TokenKind.COMMA:
                    break
                self._advance()
            self._expect(TokenKind.RPAREN, "closing IN parenthesis")
            return Membership(left, tuple(values), negated)
        if kind in (TokenKind.LIKE, TokenKind.ILIKE):
            operator = self._advance()
            return Like(left, self._value(identifier_as_field=False), operator.kind is TokenKind.LIKE)
        if kind is TokenKind.OPERATOR:
            operator = self._advance().value
            return Comparison(left, operator, self._value(identifier_as_field=False))
        if isinstance(left, FunctionCall):
            return left
        raise QuerySyntaxError("Expected comparison operator", self._peek().position)

    def _value(self, *, identifier_as_field: bool) -> Node:
        token = self._peek()
        if token.kind in (TokenKind.STRING, TokenKind.NUMBER, TokenKind.BOOL, TokenKind.NULL):
            self._advance()
            return Literal(token.value)
        if token.kind is TokenKind.IDENT:
            self._advance()
            if self._peek().kind is TokenKind.LPAREN:
                self._advance()
                args: list[Node] = []
                if self._peek().kind is not TokenKind.RPAREN:
                    while True:
                        args.append(self._value(identifier_as_field=True))
                        if self._peek().kind is not TokenKind.COMMA:
                            break
                        self._advance()
                self._expect(TokenKind.RPAREN, "closing function parenthesis")
                if token.value.lower() not in FUNCTIONS:
                    raise QuerySyntaxError(f"Unknown function {token.value!r}", token.position)
                return FunctionCall(token.value.lower(), tuple(args))
            return FieldRef(token.value) if identifier_as_field else Literal(token.value)
        raise QuerySyntaxError("Expected value", token.position)


def _resolve_field(event: LogEvent, name: str) -> Any:
    root = {
        "timestamp": event.timestamp, "level": event.level, "message": event.message,
        "service": event.service, "trace_id": event.trace_id, "span_id": event.span_id,
        "parent_span_id": event.parent_span_id, "duration_ms": event.duration_ms,
    }
    if name in root:
        return root[name]
    path = name[6:] if name.startswith("extra.") else name
    current: Any = event.extra
    for part in path.split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            value = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _args(args: list[Any], minimum: int, maximum: int | None = None) -> None:
    if len(args) < minimum or (maximum is not None and len(args) > maximum):
        raise ValueError(f"Function requires {minimum} to {maximum or 'unlimited'} arguments")


def _safe_string(value: Any) -> str:
    return "" if value is None else str(value)


def _age(args: list[Any]) -> Any:
    _args(args, 1, 1)
    dt = _coerce_datetime(args[0])
    return (datetime.now(timezone.utc) - dt).total_seconds() if dt else None


def _since(args: list[Any]) -> Any:
    seconds = _age(args)
    if seconds is None:
        return None
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _duration(args: list[Any]) -> Any:
    _args(args, 1, 2)
    before = _coerce_datetime(args[0])
    after = _coerce_datetime(args[1]) if len(args) == 2 else datetime.now(timezone.utc)
    return (after - before).total_seconds() if before and after else None


def _to_epoch(args: list[Any]) -> Any:
    _args(args, 1, 1)
    date = _coerce_datetime(args[0])
    return date.timestamp() if date else None


def _from_epoch(args: list[Any]) -> Any:
    _args(args, 1, 1)
    try:
        return datetime.fromtimestamp(float(args[0]), timezone.utc).isoformat()
    except (ValueError, OverflowError, TypeError, OSError):
        return None


def _start_of_day(args: list[Any]) -> Any:
    _args(args, 1, 1)
    date = _coerce_datetime(args[0])
    return date.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() if date else None


def _component(args: list[Any], component: str) -> Any:
    _args(args, 1, 1)
    dt = _coerce_datetime(args[0])
    return getattr(dt, component) if dt else None


def _round(args: list[Any]) -> Any:
    _args(args, 1, 2)
    try:
        return round(float(args[0]), int(args[1]) if len(args) == 2 else 0)
    except (TypeError, ValueError):
        return None


FUNCTIONS: dict[str, Callable[[list[Any]], Any]] = {
    "now": lambda args: datetime.now(timezone.utc).isoformat() if not args else None,
    "age": _age,
    "since": _since,
    "duration": _duration,
    "to_epoch": _to_epoch,
    "from_epoch": _from_epoch,
    "start_of_day": _start_of_day,
    "len": lambda a: len(a[0]) if len(a) == 1 and a[0] is not None else None,
    "lower": lambda a: _safe_string(a[0]).lower() if len(a) == 1 else None,
    "upper": lambda a: _safe_string(a[0]).upper() if len(a) == 1 else None,
    "contains": lambda a: _safe_string(a[1]) in _safe_string(a[0]) if len(a) == 2 else False,
    "startswith": lambda a: _safe_string(a[0]).startswith(_safe_string(a[1])) if len(a) == 2 else False,
    "endswith": lambda a: _safe_string(a[0]).endswith(_safe_string(a[1])) if len(a) == 2 else False,
    "exists": lambda a: a[0] is not None if len(a) == 1 else False,
    "hour": lambda a: _component(a, "hour"),
    "minute": lambda a: _component(a, "minute"),
    "day": lambda a: _component(a, "day"),
    "round": _round,
    "abs": lambda a: abs(float(a[0])) if len(a) == 1 and a[0] is not None else None,
}


def _apply_operator(left: Any, op: str, right: Any) -> bool:
    if op in ("~", "!~"):
        try:
            value = re.search(_safe_string(right), _safe_string(left)) is not None
            return value if op == "~" else not value
        except re.error:
            return False
    if isinstance(left, datetime):
        left = left.astimezone(timezone.utc).isoformat()
    if isinstance(right, datetime):
        right = right.astimezone(timezone.utc).isoformat()
    if isinstance(left, str) and isinstance(right, str):
        a, b = _coerce_datetime(left), _coerce_datetime(right)
        if a is not None and b is not None:
            left, right = a, b
    if op in ("=", "!="):
        matched = left == right
        return matched if op == "=" else not matched
    if left is None or right is None:
        return False
    try:
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            left, right = float(left), float(right)
        return {">": lambda: left > right, "<": lambda: left < right,
                ">=": lambda: left >= right, "<=": lambda: left <= right}[op]()
    except (ValueError, TypeError, KeyError):
        return False


def evaluate(node: Node, event: LogEvent) -> Any:
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, FieldRef):
        return _resolve_field(event, node.name)
    if isinstance(node, FunctionCall):
        try:
            return FUNCTIONS[node.name]([evaluate(arg, event) for arg in node.args])
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return None
    if isinstance(node, BinaryOp):
        if node.op == "AND":
            return bool(evaluate(node.left, event)) and bool(evaluate(node.right, event))
        return bool(evaluate(node.left, event)) or bool(evaluate(node.right, event))
    if isinstance(node, NotOp):
        return not bool(evaluate(node.operand, event))
    if isinstance(node, Comparison):
        left, right = evaluate(node.left, event), evaluate(node.right, event)
        if isinstance(node.left, FieldRef) and node.left.name == "level" and isinstance(left, str) and isinstance(right, str):
            left, right = left.upper(), right.upper()
        return _apply_operator(left, node.operator, right)
    if isinstance(node, Membership):
        actual = evaluate(node.left, event)
        options = [evaluate(option, event) for option in node.values]
        if isinstance(node.left, FieldRef) and node.left.name == "level":
            actual = actual.upper() if isinstance(actual, str) else actual
            options = [s.upper() if isinstance(s, str) else s for s in options]
        result = actual in options
        return not result if node.negated else result
    if isinstance(node, Like):
        value = evaluate(node.left, event)
        raw = evaluate(node.pattern, event)
        if value is None or raw is None:
            return False
        regex = '^' + re.escape(str(raw)).replace('%', '.*').replace('_', '.') + '$'
        return re.match(regex, str(value), 0 if node.case_sensitive else re.IGNORECASE) is not None
    raise TypeError(f"Unknown AST node: {type(node).__name__}")


class Query:
    """A compiled predicate reusable across log events."""
    def __init__(self, source: str) -> None:
        self.source = source or ""
        self.ast = Literal(True) if not self.source.strip() else Parser(Lexer(self.source).tokenize()).parse()

    def matches(self, event: LogEvent) -> bool:
        return bool(evaluate(self.ast, event))

    def filter(self, events: list[LogEvent]) -> list[LogEvent]:
        return [event for event in events if self.matches(event)]

    @classmethod
    def parse(cls, source: str) -> "Query":
        return cls(source)


def parse_query(source: str) -> Query:
    return Query(source)


def filter_events(events: list[LogEvent], source: str) -> list[LogEvent]:
    return Query(source).filter(events) if source and source.strip() else events
