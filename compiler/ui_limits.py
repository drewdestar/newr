"""Limits and constants for the `ui` module (SPEC.md section 12.8,
M8c).

This module holds ONLY data: the surface names and the render
format constants. It holds no rules and no diagnostics -- those live
in ``ui_check.py``. The core checker never imports this module
(SPEC.md SS2.1, the ui twin of the db/http/json rule); only
``ui_check.py`` (and the CLI, which composes the checkers, and
``docs.py``, which renders the surface) does.
"""

from __future__ import annotations

#: The error code `ui_check.py` reports (SPEC.md section 16).
UI_ERROR_CODE = "E2014"

#: The complete `ui` surface (SPEC.md section 12.8). Constructors
#: return a Widget, `render` returns a str, `show` and `run` return
#: none. `Widget` is an opaque builtin value type: only `ui.*`
#: constructors produce one, there is no Widget literal and no field
#: access on a Widget.
UI_FUNCTIONS = (
    "text", "button", "input", "list", "image", "row", "column",
    "render", "show", "run",
)

#: The `ui` surface as data (SPEC.md section 12.8). `ui_check.py`
#: validates calls against this table; `docs.py` renders it into
#: `docs/api.md`, so the generated reference and the checker can
#: never drift apart.
UI_MEMBERS: dict[str, dict[str, str]] = {
    "text": {
        "sig": "text(s: str) -> Widget",
        "doc": "A label with plain text. The render core HTML-escapes "
               "the text. Renders as `<p>text</p>`.",
    },
    "button": {
        "sig": "button(label: str, on_tap: fn() -> none) -> Widget",
        "doc": "A button with a label and a tap handler. The handler "
               "is a let-bound lambda or an inline single-expression "
               "lambda (a block-body lambda cannot be written as a "
               "call argument, E1001). The handler takes no arguments "
               "and returns none. Renders as `<button "
               "data-action=\"N\">label</button>`, where N is the "
               "button's 0-based index in pre-order traversal of the "
               "tree.",
    },
    "input": {
        "sig": "input(placeholder: str, value: str, "
               "on_change: fn(str) -> none) -> Widget",
        "doc": "One line of text with a placeholder, the current "
               "value, and an on-change handler receiving the new "
               "text. The view supplies the current value from state "
               "on every re-render; there is no two-way binding. "
               "Renders as `<input placeholder=\"...\" value=\"...\" "
               "data-input=\"N\">`, where N is the input's 0-based "
               "index in pre-order traversal of the tree.",
    },
    "list": {
        "sig": "list(items: list[Widget]) -> Widget",
        "doc": "A vertical list of widgets, normally rows. Each "
               "child is wrapped in an `<li>`. Renders as "
               "`<ul><li>...</li>...</ul>`.",
    },
    "image": {
        "sig": "image(url: str) -> Widget",
        "doc": "An image by URL. The URL must be a string literal "
               "starting with `http://` or `https://`. `ui.render` "
               "emits an `<img>` tag and never fetches the URL, so "
               "render tests need no network. A native window shows "
               "an empty placeholder when the load fails. Renders as "
               "`<img src=\"...\" alt=\"\">`.",
    },
    "row": {
        "sig": "row(children: list[Widget]) -> Widget",
        "doc": "Horizontal layout: the children in a row, in order. "
               "No spacing, alignment, or sizing arguments in v1. "
               "Renders as `<div data-layout=\"row\">...</div>`.",
    },
    "column": {
        "sig": "column(children: list[Widget]) -> Widget",
        "doc": "Vertical layout: the children in a column, in order. "
               "No spacing, alignment, or sizing arguments in v1. "
               "Renders as `<div data-layout=\"column\">...</div>`.",
    },
    "render": {
        "sig": "render(root: Widget) -> str",
        "doc": "Renders a widget tree to a deterministic HTML "
               "string. Pure: no window, no network, no side "
               "effects. The same tree renders to the same bytes on "
               "every target and every run (SPEC.md section 12.8 "
               "pins the byte format). This is the conformance-"
               "testable core of the module.",
    },
    "show": {
        "sig": "show(root: Widget) -> none",
        "doc": "Opens the tree in a native window where a toolkit "
               "exists (py, java, kt, cs, rs) and blocks until the "
               "window closes. On js and dart it writes the rendered "
               "HTML to a file next to the program and prints the "
               "path, then returns. At most one of `show` and `run` "
               "may be called in a program; a program has one window.",
    },
    "run": {
        "sig": "run(view: fn() -> Widget) -> none",
        "doc": "The interactive loop. Takes a view function, not a "
               "tree: the runtime calls the view, renders the tree, "
               "dispatches one event to its handler, then calls the "
               "view again and re-renders. State lives outside the "
               "tree (in the db module or an http backend); a handler "
               "never assigns to a captured variable (that assignment "
               "is a silent no-op, SPEC.md section 12.8). The loop "
               "ends when the window closes. On js and dart it "
               "renders once and returns; the interactive loop is a "
               "documented no-op on those targets.",
    },
}
