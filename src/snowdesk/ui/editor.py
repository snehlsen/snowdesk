"""Monospace SQL editor with line numbers (E1)."""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from snowdesk.ui.highlighter import SqlHighlighter


class _LineNumberArea(QWidget):
    def __init__(self, editor: SqlEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        self._editor.paint_line_numbers(event)


class SqlEditor(QPlainTextEdit):
    """A plain-text SQL editor: line numbers, current-line highlight, and a
    marker for the statement that failed (Q5)."""

    run_requested = Signal()  # ⌘↩
    run_all_requested = Signal()  # ⌘⇧↩

    def __init__(self, parent: QWidget | None = None, dark: bool = False) -> None:
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(13)
        self.setFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setPlaceholderText("-- ⌘↩ runs the statement under the cursor, ⌘⇧↩ runs everything")

        self.highlighter = SqlHighlighter(self.document(), dark=dark)
        self._line_numbers = _LineNumberArea(self)
        self._error_range: tuple[int, int] | None = None
        self._dark = dark

        self.blockCountChanged.connect(lambda _: self._update_margins())
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self._refresh_extra_selections)
        self._update_margins()
        self._refresh_extra_selections()

    # -- line numbers ------------------------------------------------------

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margins(self) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_numbers.scroll(0, dy)
        else:
            self._line_numbers.update(0, rect.y(), self._line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margins()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_numbers.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def paint_line_numbers(self, event: QPaintEvent) -> None:
        painter = QPainter(self._line_numbers)
        palette = self.palette()
        painter.fillRect(event.rect(), palette.window().color())
        painter.setPen(QColor("#8a8f98"))

        block = self.firstVisibleBlock()
        number = block.blockNumber()
        offset = self.contentOffset()
        top = self.blockBoundingGeometry(block).translated(offset).top()
        height = self.blockBoundingRect(block).height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(
                    0,
                    int(top),
                    self._line_numbers.width() - 6,
                    int(height),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(number + 1),
                )
            block = block.next()
            top += height
            height = self.blockBoundingRect(block).height()
            number += 1

    # -- selections --------------------------------------------------------

    def set_font_size(self, points: int) -> None:
        font = self.font()
        font.setPointSize(points)
        self.setFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self._update_margins()

    def set_dark(self, dark: bool) -> None:
        """Re-theme in place, so a running app can change appearance."""
        self._dark = dark
        self.highlighter.set_dark(dark)
        self._refresh_extra_selections()

    def selection_range(self) -> tuple[int, int]:
        cursor = self.textCursor()
        return cursor.selectionStart(), cursor.selectionEnd()

    def mark_error(self, start: int, end: int) -> None:
        """Underline the failing statement (Q5)."""
        self._error_range = (start, end)
        self._refresh_extra_selections()

    def clear_error(self) -> None:
        self._error_range = None
        self._refresh_extra_selections()

    def _refresh_extra_selections(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []

        current = QTextEdit.ExtraSelection()
        current.format.setBackground(
            QColor(255, 255, 255, 18) if self._dark else QColor(0, 0, 0, 10)
        )
        current.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        cursor = self.textCursor()
        cursor.clearSelection()
        current.cursor = cursor
        selections.append(current)

        if self._error_range is not None:
            start, end = self._error_range
            error = QTextEdit.ExtraSelection()
            error.format.setUnderlineColor(QColor("#e5534b"))
            error.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            error.format.setBackground(QColor(229, 83, 75, 28))
            error_cursor = QTextCursor(self.document())
            error_cursor.setPosition(max(0, start))
            error_cursor.setPosition(
                min(end, len(self.toPlainText())), QTextCursor.MoveMode.KeepAnchor
            )
            error.cursor = error_cursor
            selections.append(error)

        self.setExtraSelections(selections)

    # -- input -------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.run_all_requested.emit()
            else:
                self.run_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def insert_identifier(self, text: str) -> None:
        """Insert text at the cursor, used by the object browser (B3)."""
        cursor = self.textCursor()
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.setFocus()
