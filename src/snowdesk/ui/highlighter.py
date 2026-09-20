"""Syntax highlighting for Snowflake SQL (E1)."""

from __future__ import annotations

import re

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

KEYWORDS = [
    "ALTER",
    "AND",
    "AS",
    "ASC",
    "BEGIN",
    "BETWEEN",
    "BY",
    "CASE",
    "CAST",
    "CLONE",
    "COMMIT",
    "COPY",
    "CREATE",
    "CROSS",
    "CURRENT",
    "DATABASE",
    "DELETE",
    "DESC",
    "DESCRIBE",
    "DISTINCT",
    "DROP",
    "ELSE",
    "END",
    "EXCEPT",
    "EXISTS",
    "EXPLAIN",
    "FALSE",
    "FETCH",
    "FILE",
    "FLATTEN",
    "FOLLOWING",
    "FORMAT",
    "FROM",
    "FULL",
    "GET",
    "GRANT",
    "GROUP",
    "HAVING",
    "ILIKE",
    "IN",
    "INNER",
    "INSERT",
    "INTERSECT",
    "INTO",
    "IS",
    "JOIN",
    "LATERAL",
    "LEFT",
    "LIKE",
    "LIMIT",
    "MERGE",
    "NATURAL",
    "NOT",
    "NULL",
    "NULLS",
    "OFFSET",
    "ON",
    "OR",
    "ORDER",
    "OUTER",
    "OVER",
    "PARTITION",
    "PIVOT",
    "PRECEDING",
    "PUT",
    "QUALIFY",
    "RANGE",
    "RIGHT",
    "ROLE",
    "ROLLBACK",
    "ROWS",
    "SAMPLE",
    "SCHEMA",
    "SELECT",
    "SET",
    "SHOW",
    "STAGE",
    "STREAM",
    "SWAP",
    "TABLE",
    "TASK",
    "THEN",
    "TOP",
    "TRANSIENT",
    "TRUE",
    "TRUNCATE",
    "UNBOUNDED",
    "UNION",
    "UNPIVOT",
    "UPDATE",
    "USE",
    "USING",
    "VALUES",
    "VIEW",
    "WAREHOUSE",
    "WHEN",
    "WHERE",
    "WINDOW",
    "WITH",
]

TYPES = [
    "ARRAY",
    "BIGINT",
    "BINARY",
    "BOOLEAN",
    "CHAR",
    "DATE",
    "DECIMAL",
    "DOUBLE",
    "FLOAT",
    "GEOGRAPHY",
    "GEOMETRY",
    "INT",
    "INTEGER",
    "NUMBER",
    "NUMERIC",
    "OBJECT",
    "REAL",
    "SMALLINT",
    "STRING",
    "TEXT",
    "TIME",
    "TIMESTAMP",
    "TIMESTAMP_LTZ",
    "TIMESTAMP_NTZ",
    "TIMESTAMP_TZ",
    "VARCHAR",
    "VARIANT",
    "VECTOR",
]

FUNCTIONS = [
    "ABS",
    "ANY_VALUE",
    "ARRAY_AGG",
    "AVG",
    "COALESCE",
    "CONCAT",
    "COUNT",
    "CURRENT_DATE",
    "CURRENT_DATABASE",
    "CURRENT_ROLE",
    "CURRENT_SCHEMA",
    "CURRENT_TIMESTAMP",
    "CURRENT_WAREHOUSE",
    "DATEADD",
    "DATEDIFF",
    "DATE_TRUNC",
    "IFF",
    "IFNULL",
    "LAG",
    "LEAD",
    "LISTAGG",
    "LOWER",
    "MAX",
    "MIN",
    "NVL",
    "OBJECT_CONSTRUCT",
    "PARSE_JSON",
    "RANK",
    "ROW_NUMBER",
    "SPLIT",
    "SUM",
    "TO_CHAR",
    "TO_DATE",
    "TO_JSON",
    "TO_NUMBER",
    "TO_TIMESTAMP",
    "TRIM",
    "TRY_CAST",
    "TRY_PARSE_JSON",
    "UPPER",
]


def _fmt(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(QFont.Weight.DemiBold)
    if italic:
        fmt.setFontItalic(True)
    return fmt


class SqlHighlighter(QSyntaxHighlighter):
    """Keywords, types, functions, literals and comments.

    Colours are chosen to stay legible on both light and dark backgrounds
    rather than being tuned for one theme.
    """

    def __init__(self, document: QTextDocument, dark: bool = False) -> None:
        super().__init__(document)
        self.set_dark(dark)

    def set_dark(self, dark: bool) -> None:
        """Rebuild the formats for a new appearance and repaint the document."""
        keyword = _fmt("#7aa2f7" if dark else "#0b5cad", bold=True)
        type_fmt = _fmt("#bb9af7" if dark else "#7a3ea3")
        func = _fmt("#7dcfff" if dark else "#0a7285")
        self._string = _fmt("#9ece6a" if dark else "#116329")
        self._number = _fmt("#ff9e64" if dark else "#a15c00")
        self._comment = _fmt("#767b91" if dark else "#6a737d", italic=True)

        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        for words, fmt in ((KEYWORDS, keyword), (TYPES, type_fmt), (FUNCTIONS, func)):
            pattern = r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b"
            expr = QRegularExpression(pattern)
            expr.setPatternOptions(QRegularExpression.PatternOption.CaseInsensitiveOption)
            self._rules.append((expr, fmt))

        self._rules.append((QRegularExpression(r"\b\d+(?:\.\d+)?\b"), self._number))
        self._rules.append((QRegularExpression(r"'(?:[^']|'')*'"), self._string))
        self._rules.append((QRegularExpression(r'"(?:[^"]|"")*"'), self._string))
        self._rules.append((QRegularExpression(r"(--|//)[^\n]*"), self._comment))

        self._block_start = QRegularExpression(r"/\*")
        self._block_end = QRegularExpression(r"\*/")
        if self.document() is not None:
            self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        for expr, fmt in self._rules:
            it = expr.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)
        self._highlight_block_comments(text)

    def _highlight_block_comments(self, text: str) -> None:
        start = 0 if self.previousBlockState() == 1 else -1
        if start < 0:
            match = self._block_start.match(text)
            start = match.capturedStart() if match.hasMatch() else -1
        while start >= 0:
            end_match = self._block_end.match(text, start)
            if end_match.hasMatch():
                length = end_match.capturedEnd() - start
                self.setCurrentBlockState(0)
            else:
                length = len(text) - start
                self.setCurrentBlockState(1)
            self.setFormat(start, length, self._comment)
            next_match = self._block_start.match(text, start + length)
            start = next_match.capturedStart() if next_match.hasMatch() else -1
