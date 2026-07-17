"""Prompt input widget for the Relay TUI."""

from __future__ import annotations

from textual.widgets import Input

class PromptInput(Input):
    """The main prompt input. When the slash popover is open it routes up/down/esc
    to the popover (Enter is handled via ``Input.Submitted`` in the app)."""

    def _on_paste(self, event) -> None:
        """Capture a multi-line paste IN FULL.

        Textual's ``Input._on_paste`` keeps only ``event.text.splitlines()[0]`` --
        silently dropping every line after the first. That corrupts a pasted
        multi-line goal/spec (Relay sees only line one). We insert the WHOLE pasted
        text instead. A newline WITHIN a paste is content, never a submit: the paste
        arrives as one ``Paste`` event (not a stream of Enter keypresses), so this
        does not submit and does not touch the explicit Enter-to-submit path for
        typed input.

        ``prevent_default()`` is essential: Textual dispatches an event to EVERY
        matching handler in the MRO, so without it the base ``Input._on_paste``
        would still run after this one and re-append the truncated first line.
        """
        text = event.text
        if text:
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(text)
            else:
                self.replace(text, *selection)
        event.prevent_default()  # suppress the base (first-line-only) paste handler
        event.stop()

    def on_key(self, event) -> None:
        app = self.app
        # While the slash popover is open, up/down move the highlight and esc closes it.
        if getattr(app, "_popover_open", False):
            if event.key == "down":
                app._popover_move(1); event.prevent_default(); event.stop()
            elif event.key == "up":
                app._popover_move(-1); event.prevent_default(); event.stop()
            elif event.key == "escape":
                app._popover_close(); event.prevent_default(); event.stop()
            return
        # Otherwise up/down are the ONE unified recall-and-edit affordance: walk the
        # input history (goals, steers, queued items) into the field for editing.
        # Shift+Enter inserts a newline (U4 multi-line composer) without submitting.
        if event.key == "shift+enter":
            self.insert_text_at_cursor("\n")
            event.prevent_default()
            event.stop()
            return
        if event.key == "up":
            recalled = app._recall_older()
            if recalled is not None:
                self.value = recalled
                self.cursor_position = len(self.value)
            event.prevent_default(); event.stop()
        elif event.key == "down":
            recalled = app._recall_newer()
            if recalled is not None:
                self.value = recalled
                self.cursor_position = len(self.value)
            event.prevent_default(); event.stop()

