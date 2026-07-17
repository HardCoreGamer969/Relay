"""Reusable dialog widgets for slash commands and setup flows."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker, WorkerState

from .theme import C_DIM

class FilterInput(Input):
    """A dialog's filter field: up/down move the dialog highlight (the screen owns
    selection); typing filters via the screen's ``apply_filter``."""

    def on_key(self, event) -> None:
        screen = self.screen
        if event.key == "down" and hasattr(screen, "move"):
            screen.move(1); event.prevent_default(); event.stop()
        elif event.key == "up" and hasattr(screen, "move"):
            screen.move(-1); event.prevent_default(); event.stop()


_DIALOG_CSS = """
SelectDialog, TextEntryDialog, SegmentedControl { align: center middle; }
#dialog-box {
    width: 80%; max-width: 100; height: auto; max-height: 90%;
    padding: 1 2; border: double $primary; background: $surface;
}
#dialog-title { text-style: bold; content-align: center middle; }
#dialog-list { margin: 1 0; }
#segment-row { margin: 1 0; content-align: center middle; }
#dialog-hint, #entry-hint { color: $text-muted; text-style: dim; margin-top: 1; }
#dialog-filter, #entry-input { margin-bottom: 1; }
#entry-status { margin-top: 1; }
"""


class SelectDialog(ModalScreen):
    """One generic filterable selection dialog -- the primitive every list command
    (``/help``, ``/model``, ``/config``, ``/doctor``, ``/runs``, ``/assume``) opens.

    ``options`` is a list of dicts: ``{title, value, description?, category?,
    on_select?}``. Options are grouped by ``category`` when present; typing filters,
    arrows move, Enter calls the highlighted option's ``on_select(value)``.
    """

    BINDINGS = [("escape", "close", "Close")]
    CSS = _DIALOG_CSS

    def __init__(self, *, title: str, options: list[dict]) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._visible: list[dict] = list(self._options)
        self._highlight = 0

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog-box"):
            yield Static(self._title, id="dialog-title")
            yield FilterInput(placeholder="type to filter...", id="dialog-filter")
            yield Static(id="dialog-list")
            yield Static("up/down move  ·  enter choose  ·  esc close", id="dialog-hint")

    def on_mount(self) -> None:
        self.apply_filter("")
        self.query_one("#dialog-filter", Input).focus()

    # -- testable core --------------------------------------------------------

    def apply_filter(self, text: str) -> None:
        q = (text or "").strip().lower()

        def match(option: dict) -> bool:
            hay = " ".join(
                str(option.get(k, "")) for k in ("title", "value", "description", "category")
            ).lower()
            return not q or q in hay

        self._visible = [o for o in self._options if match(o)]
        self._highlight = 0
        self._refresh_list()

    def visible_values(self) -> list:
        return [o.get("value") for o in self._visible]

    def move(self, delta: int) -> None:
        if not self._visible:
            return
        self._highlight = max(0, min(len(self._visible) - 1, self._highlight + delta))
        self._refresh_list()

    def select_highlighted(self) -> None:
        if self._visible:
            self.choose(self._visible[self._highlight].get("value"))

    def choose(self, value) -> None:
        """Dismiss and invoke the chosen option's ``on_select`` (if any)."""
        chosen = next((o for o in self._visible if o.get("value") == value), None)
        if chosen is None:
            return
        self.dismiss()
        callback = chosen.get("on_select")
        if callback is not None:
            callback(value)

    # -- rendering ------------------------------------------------------------

    def _refresh_list(self) -> None:
        # NOTE: do NOT name this ``_render`` -- that shadows Textual's
        # ``Widget._render`` (which must return a Visual) and renders the screen None.
        try:
            widget = self.query_one("#dialog-list", Static)
        except Exception:  # noqa: BLE001 -- not mounted (headless logic-only use)
            return
        widget.update(self._list_renderable())

    def _list_renderable(self) -> Text:
        text = Text()
        if not self._visible:
            text.append("(no matches)", style="dim")
            return text
        last_category = object()
        for i, option in enumerate(self._visible):
            category = option.get("category")
            if category and category != last_category:
                text.append(f"{category}\n", style="bold")
                last_category = category
            marker = "> " if i == self._highlight else "  "
            style = "reverse" if i == self._highlight else ""
            line = f"{marker}{option.get('title', option.get('value', ''))}"
            text.append(line, style=style)
            desc = option.get("description")
            if desc:
                text.append(f"  -  {desc}", style="dim")
            text.append("\n")
        return text

    # -- widget wiring --------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "dialog-filter":
            event.stop()
            self.apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "dialog-filter":
            event.stop()
            self.select_highlighted()

    def action_close(self) -> None:
        self.dismiss()


class TextEntryDialog(ModalScreen):
    """A single-field entry dialog -- masked (``password=True``) for a key, plain
    for a manual model slug. ``on_submit(value) -> (ok, note)``; the dialog stays
    open (showing the note) on failure, dismisses on success. The value is read
    ONLY from this dialog's own field -- never from the chat prompt.

    When ``async_submit=True``, validation/persist runs off the UI thread so a live
    network probe cannot freeze Textual.
    """

    BINDINGS = [("escape", "close", "Close")]
    CSS = _DIALOG_CSS

    def __init__(
        self, *, title: str, label: str, on_submit, password: bool = False,
        placeholder: str = "", async_submit: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._label = label
        self._on_submit = on_submit
        self._password = password
        self._placeholder = placeholder
        self._async_submit = async_submit
        self._submitting = False
        self.status_text = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog-box"):
            yield Static(self._title, id="dialog-title")
            yield Label(self._label)
            yield Input(password=self._password, placeholder=self._placeholder, id="entry-input")
            yield Button("Save", id="entry-save", variant="primary")
            yield Static("", id="entry-status")
            yield Static("enter to save  ·  esc to cancel", id="entry-hint")

    def on_mount(self) -> None:
        self.query_one("#entry-input", Input).focus()

    def submit(self) -> bool:
        """Read THIS dialog's field and hand it to ``on_submit``. Returns saved?."""
        if self._submitting:
            return False
        value = self.query_one("#entry-input", Input).value
        if self._async_submit:
            self._submitting = True
            self._set_status("[dim]validating…[/dim]")
            def work():
                return self._on_submit(value)
            self.run_worker(
                work, thread=True, name="text-entry-submit",
                group="text-entry-submit", exclusive=True, exit_on_error=False,
            )
            return False
        ok, note = self._on_submit(value)
        if ok:
            self.dismiss()
            return True
        self._set_status(f"[red]{note}[/red]")
        return False

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if (event.worker.name or "") != "text-entry-submit":
            return
        if event.state is WorkerState.SUCCESS:
            ok, note = event.worker.result or (False, "validation failed")
            self._submitting = False
            if ok:
                self.dismiss()
            else:
                self._set_status(f"[red]{note}[/red]")
        elif event.state in (WorkerState.ERROR, WorkerState.CANCELLED):
            self._submitting = False
            self._set_status("[red]validation failed[/red]")

    def _set_status(self, message: str) -> None:
        self.status_text = message
        try:
            self.query_one("#entry-status", Static).update(message)
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "entry-save":
            event.stop()
            self.submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "entry-input":
            event.stop()
            self.submit()

    def action_close(self) -> None:
        self.dismiss()


class SegmentRow(Static):
    """The focusable key-sink for a :class:`SegmentedControl` (no text field, so
    the row itself takes focus and routes left/right/enter/escape to the screen)."""

    can_focus = True

    def on_key(self, event) -> None:
        screen = self.screen
        if not hasattr(screen, "move"):
            return
        if event.key in ("left", "h"):
            screen.move(-1); event.prevent_default(); event.stop()
        elif event.key in ("right", "l"):
            screen.move(1); event.prevent_default(); event.stop()
        elif event.key == "enter":
            screen.select_highlighted(); event.prevent_default(); event.stop()
        elif event.key == "escape":
            screen.action_close(); event.prevent_default(); event.stop()


class SegmentedControl(ModalScreen):
    """A reusable horizontal choose-one toggle (the analog of :class:`SelectDialog`
    for a small fixed set picked with LEFT/RIGHT, with wrap-around).

    ``options`` is an ordered list of ``{label, value}``; LEFT/RIGHT move the
    highlight (wrapping at both ends), Enter commits the highlighted option (calls
    ``on_select(value)`` then dismisses), Esc cancels. It's a ModalScreen (same CSS
    family / aesthetic as the other dialogs), so it never touches the prompt input
    or the InputRouter. The testable core (``move`` / ``highlighted_value`` /
    ``select_highlighted``) is kept separate from rendering -- mirroring SelectDialog.
    """

    BINDINGS = [
        ("left", "move_left", "Prev"),
        ("right", "move_right", "Next"),
        ("escape", "close", "Cancel"),
    ]
    CSS = _DIALOG_CSS

    def __init__(
        self, *, title: str, options: list[dict], start_index: int = 0, on_select=None
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        n = len(self._options)
        self._index = (start_index % n) if n else 0
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog-box"):
            yield Static(self._title, id="dialog-title")
            yield SegmentRow(id="segment-row")
            yield Static("left/right to choose  ·  enter to confirm  ·  esc to cancel",
                         id="dialog-hint")

    def on_mount(self) -> None:
        self._refresh_segments()
        self.query_one("#segment-row", SegmentRow).focus()

    # -- testable core (no rendering) ----------------------------------------

    def move(self, delta: int) -> None:
        """Move the highlight by ``delta`` with WRAP-AROUND at both ends."""
        n = len(self._options)
        if n == 0:
            return
        self._index = (self._index + delta) % n
        self._refresh_segments()

    def highlighted_value(self):
        """The currently highlighted option's value (``None`` if there are none)."""
        if not self._options:
            return None
        return self._options[self._index].get("value")

    def select_highlighted(self) -> None:
        """Commit the highlighted option: dismiss, then call ``on_select(value)``."""
        if not self._options:
            self.dismiss()
            return
        value = self._options[self._index].get("value")
        self.dismiss()
        if self._on_select is not None:
            self._on_select(value)

    # -- rendering ------------------------------------------------------------

    def _refresh_segments(self) -> None:
        try:
            self.query_one("#segment-row", SegmentRow).update(self._segments_text())
        except Exception:  # noqa: BLE001 -- not mounted (logic-only use in tests)
            pass

    def _segments_text(self) -> Text:
        text = Text()
        if not self._options:
            text.append("(no options)", style="dim")
            return text
        for i, option in enumerate(self._options):
            if i:
                text.append("  <  >  ", style="dim")  # the toggle's left/right hint
            label = str(option.get("label", option.get("value", "")))
            if i == self._index:
                text.append(f"[ {label} ]", style="reverse bold")
            else:
                text.append(f"  {label}  ")
        return text

    # -- key actions (real-terminal bindings; tests drive the core directly) --

    def action_move_left(self) -> None:
        self.move(-1)

    def action_move_right(self) -> None:
        self.move(1)

    def action_close(self) -> None:
        self.dismiss()


class ApproveDialog(ModalScreen):
    """Dedicated approval modal (U4): command + reason + once / session / deny.

    Settles via ``on_decision(action)`` where action is ``once`` | ``session`` | ``deny``.
    """

    BINDINGS = [
        ("escape", "deny", "Deny"),
        ("1", "once", "Once"),
        ("2", "session", "Session"),
        ("3", "deny", "Deny"),
        ("y", "once", "Once"),
        ("n", "deny", "Deny"),
    ]
    CSS = _DIALOG_CSS + """
    #approve-cmd { margin: 1 0; color: #f0f0f0; }
    #approve-reason { color: #888888; }
    #approve-diff { margin-top: 1; max-height: 12; }
    """

    def __init__(
        self,
        *,
        command: str,
        reason: str,
        on_decision=None,
        diff: str = "",
    ) -> None:
        super().__init__()
        self._command = command or ""
        self._reason = reason or ""
        self._diff = diff or ""
        self._on_decision = on_decision
        self.decision: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog-box"):
            yield Static("Approve gated command", id="dialog-title")
            yield Static(self._command, id="approve-cmd")
            yield Static(f"Why: {self._reason}" if self._reason else "", id="approve-reason")
            if self._diff:
                yield Static(self._diff[:2000], id="approve-diff")
            yield Static(
                "[1] once   [2] session allow   [3] deny   ·  esc deny",
                id="dialog-hint",
            )

    def _finish(self, action: str) -> None:
        self.decision = action
        cb = self._on_decision
        self.dismiss(action)
        if cb is not None:
            cb(action)

    def action_once(self) -> None:
        self._finish("once")

    def action_session(self) -> None:
        self._finish("session")

    def action_deny(self) -> None:
        self._finish("deny")

