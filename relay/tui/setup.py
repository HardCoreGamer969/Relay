"""Provider setup screen and persistence helpers for the Relay TUI."""

from __future__ import annotations

import sys

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static
from textual.worker import Worker, WorkerState

from relay.config import ROLES, ModelConfig, default_config, describe_resolution
from relay.providers import (
    DISCOVERY_LIST,
    known_providers,
    list_models as provider_list_models,
    resolve_provider,
    validate_model as provider_validate_model,
)
from relay.secrets import set_key as secrets_set_key
from relay.store import CONFIG_VERSION, load_config, save_config

from .events import friendly_provider_error


def _public_attr(name: str, default):
    public = sys.modules.get("relay.tui")
    return getattr(public, name, default) if public is not None else default


def setup_summary() -> str:
    """A plain, key-free summary of the current resolution (provider/model/key
    presence per role/provider). Reads :func:`describe_resolution` -- NEVER a key."""
    res = describe_resolution()
    lines = []
    for role in ROLES:
        f = res["roles"][role]
        thinking = "on" if f["thinking"][0] else "off"
        lines.append(
            f"{role}: {f['provider'][0]} / {f['model'][0]}  (thinking {thinking}; "
            f"src {f['provider'][1]}/{f['model'][1]})"
        )
    for pid in known_providers():
        present = res["providers"][pid]["key_present"]
        lines.append(f"key[{pid}]: {'present' if present else 'absent'}")
    return "\n".join(lines)


def persist_role(
    role: str, provider: str, model: str, thinking: bool, *, validate_fn=None
) -> tuple[bool, str]:
    """Validate a (provider, model) live, then persist the role to config.json.

    The ONE place a role selection is written -- shared by the SetupScreen and the
    ``/model`` slash command so they can never fork (same validation, same write).
    ``validate_fn`` defaults to the shared :func:`relay.providers.validate_model`.
    Returns ``(saved?, note)``; does not persist on validation failure.
    """
    validate_fn = validate_fn or provider_validate_model
    model = (model or "").strip()
    if not model:
        return False, "enter a model id"
    ok, note = validate_fn(provider, model)
    if not ok:
        return False, note
    config = load_config() or default_config()
    config.setdefault("version", CONFIG_VERSION)
    config.setdefault("roles", {})[role] = {
        "provider": provider, "model": model, "thinking": bool(thinking),
    }
    save_config(config)
    return True, note


def _call_persist_role(role: str, provider: str, model: str, thinking: bool, *, validate_fn=None):
    return _public_attr("persist_role", persist_role)(
        role, provider, model, thinking, validate_fn=validate_fn
    )


def _call_secrets_set_key(provider: str, key: str) -> None:
    _public_attr("secrets_set_key", secrets_set_key)(provider, key)


class SetupScreen(ModalScreen):
    """In-TUI provider setup: enter a key (masked), pick per-role models, toggle
    thinking -- for a beta user with no terminal/.env knowledge.

    All persistence goes through the Part-1 backend (auth.json 0o600 for keys,
    config.json for selections). Network-touching work (model listing, slug
    validation) is behind injectable seams so the screen is headless-testable and
    never hits the network in tests. Real unicode; consistent cyberpunk aesthetic.

    U1: ``compose()`` never calls the network -- model lists load via a thread
    worker after mount (and again when the provider Select changes).
    """

    BINDINGS = [("escape", "close", "Close setup")]

    CSS = """
    SetupScreen { align: center middle; }
    #setup-box {
        width: 80%; max-width: 100; height: auto; max-height: 90%;
        padding: 1 2; border: double $primary; background: $surface;
    }
    #setup-title { text-style: bold; content-align: center middle; }
    #setup-summary { color: $text-muted; margin: 1 0; }
    #setup-status { margin-top: 1; }
    .setup-section { margin-top: 1; text-style: bold; color: $secondary; }
    Select, Input, Checkbox { margin-bottom: 1; }
    """

    def __init__(
        self,
        *,
        models: ModelConfig,
        list_models_fn=None,
        validate_fn=None,
        on_saved=None,
    ) -> None:
        super().__init__()
        self._models = models
        # Seams (injected by tests; default to the real, network-touching funcs).
        self._list_models_fn = list_models_fn or provider_list_models
        self._validate_fn = validate_fn or provider_validate_model
        self._on_saved = on_saved
        self._provider_options = [(p, p) for p in known_providers()]
        # The last status message rendered (mirrored for headless tests).
        self.status_text = ""

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="setup-box"):
            yield Static("Relay setup", id="setup-title")
            yield Static(setup_summary(), id="setup-summary")

            yield Label("Provider key", classes="setup-section")
            yield Select(self._provider_options, id="key-provider", allow_blank=False,
                         value=self._models.brain_provider)
            # password=True -> the field shows bullets; keys get screenshotted.
            yield Input(placeholder="paste the API key (hidden)", password=True, id="key-input")
            yield Button("Save key", id="save-key", variant="primary")

            for role in ROLES:
                provider = self._models.provider_for_role(role)
                yield Label(f"{role} model", classes="setup-section")
                yield Select(self._provider_options, id=f"{role}-provider",
                             allow_blank=False, value=provider)
                yield Input(value=self._models.for_role(role),
                            placeholder="model id / slug", id=f"{role}-model")
                # Empty at compose time -- populated off-thread in on_mount /
                # provider-change (U1: no live HTTP on the UI thread during compose).
                yield Select([], id=f"{role}-model-list", allow_blank=True)
                yield Checkbox("thinking", value=self._models.thinking_for_role(role),
                               id=f"{role}-thinking")
                yield Button(f"Save {role}", id=f"save-{role}")

            yield Static("", id="setup-status")
            yield Static("openrouter: type any slug  ·  deepseek: pick from the list  ·  esc to close",
                         id="setup-hint")

    def on_mount(self) -> None:
        for role in ROLES:
            self._start_fetch_models(role, self._models.provider_for_role(role))

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Apply off-thread model-list / save-role results."""
        name = event.worker.name or ""
        if name.startswith("setup-save-") and event.state is WorkerState.SUCCESS:
            role = name[len("setup-save-"):]
            ok, note = event.worker.result or (False, "save failed")
            if not ok:
                note = friendly_provider_error(note)
                self._set_status(f"[red]{role} rejected:[/red] {note}")
                return
            self._set_status(f"[green]saved {role}.[/green]")
            self._refresh_summary()
            self._notify_saved()
            return
        if event.state is not WorkerState.SUCCESS:
            return
        if not name.startswith("setup-models-"):
            return
        # Name format: setup-models-{role}::{provider} — ignore stale completes.
        rest = name[len("setup-models-"):]
        if "::" not in rest:
            return
        role, provider = rest.split("::", 1)
        try:
            current = str(self.query_one(f"#{role}-provider", Select).value)
        except Exception:  # noqa: BLE001 -- torn down
            return
        if current != provider:
            return
        ids = list(event.worker.result or [])
        self._apply_model_options(role, ids)

    # -- seams + helpers (testable) ------------------------------------------

    def _model_options(self, role: str, provider: str) -> list[tuple[str, str]]:
        """Selectable model-id options for a role's provider (``[]`` for manual)."""
        return [(mid, mid) for mid in self.models_for(provider)]

    def models_for(self, provider: str) -> list[str]:
        """Live model ids for a ``list`` provider (``[]`` for manual / on error)."""
        try:
            profile = resolve_provider(provider)
        except ValueError:
            return []
        if profile.discovery != DISCOVERY_LIST:
            return []
        try:
            return list(self._list_models_fn(provider))
        except Exception:  # noqa: BLE001 -- no key/network: just an empty list
            return []

    def _start_fetch_models(self, role: str, provider: str) -> None:
        """Kick a named thread worker so results can be routed per role.

        Stamp the provider into the worker name and use an exclusive per-role
        group so a slow prior fetch cannot overwrite a newer provider's list.
        """
        def fetch() -> list[str]:
            return self.models_for(provider)

        self.run_worker(
            fetch,
            thread=True,
            name=f"setup-models-{role}::{provider}",
            group=f"setup-models-{role}",
            exclusive=True,
            exit_on_error=False,
        )

    def _apply_model_options(self, role: str, ids: list[str]) -> None:
        try:
            select = self.query_one(f"#{role}-model-list", Select)
        except Exception:  # noqa: BLE001 -- not mounted / torn down
            return
        select.set_options([(mid, mid) for mid in ids])

    def save_key(self, provider: str, key: str) -> bool:
        """Store a key (masked-entered) to auth.json 0o600. Returns saved?."""
        key = (key or "").strip()
        if not key:
            self._set_status("[yellow]no key entered.[/yellow]")
            return False
        _call_secrets_set_key(provider, key)  # the value is NEVER echoed back
        self._set_status(f"[green]stored a key for {provider}.[/green]")
        self._refresh_summary()
        self._notify_saved()
        return True

    def save_role(self, role: str, provider: str, model: str, thinking: bool) -> bool:
        """Validate (live) and persist a role's provider/model/thinking. Returns saved?.

        Delegates to the shared :func:`persist_role` (same path the ``/model`` slash
        command uses) so validation + persistence never fork.
        """
        ok, note = _call_persist_role(role, provider, model, thinking, validate_fn=self._validate_fn)
        if not ok:
            note = friendly_provider_error(note, provider=provider, model=model)
            self._set_status(f"[red]{role} rejected:[/red] {note}")  # inline error, not saved
            return False
        self._set_status(f"[green]saved {role}: {provider} / {model}.[/green]")
        self._refresh_summary()
        self._notify_saved()
        return True

    # -- widget event wiring --------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        sid = event.select.id or ""
        if sid.endswith("-provider") and not sid.startswith("key"):
            role = sid[: -len("-provider")]
            self._repopulate_model_list(role, str(event.value))
        elif sid.endswith("-model-list") and event.value not in (None, Select.BLANK):
            role = sid[: -len("-model-list")]
            self.query_one(f"#{role}-model", Input).value = str(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "save-key":
            provider = str(self.query_one("#key-provider", Select).value)
            self.save_key(provider, self.query_one("#key-input", Input).value)
            self.query_one("#key-input", Input).value = ""  # don't leave the key on screen
        elif bid.startswith("save-"):
            self._save_role_from_widgets(bid[len("save-"):])

    def _save_role_from_widgets(self, role: str) -> None:
        if role not in ROLES:
            return
        provider = str(self.query_one(f"#{role}-provider", Select).value)
        model = self.query_one(f"#{role}-model", Input).value
        thinking = bool(self.query_one(f"#{role}-thinking", Checkbox).value)
        # Live validate+persist off the UI thread (setup screen Save button).
        self._set_status(f"[dim]validating {role}…[/dim]")
        validate_fn = self._validate_fn

        def work():
            return _call_persist_role(
                role, provider, model, thinking, validate_fn=validate_fn
            )

        self.run_worker(
            work,
            thread=True,
            name=f"setup-save-{role}",
            group=f"setup-save-{role}",
            exclusive=True,
            exit_on_error=False,
        )

    def _repopulate_model_list(self, role: str, provider: str) -> None:
        try:
            select = self.query_one(f"#{role}-model-list", Select)
        except Exception:  # noqa: BLE001 -- not mounted yet
            return
        select.set_options([])  # clear immediately; refill off-thread
        self._start_fetch_models(role, provider)

    def _refresh_summary(self) -> None:
        try:
            self.query_one("#setup-summary", Static).update(setup_summary())
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def _set_status(self, message: str) -> None:
        self.status_text = message
        try:
            self.query_one("#setup-status", Static).update(message)
        except Exception:  # noqa: BLE001 -- not mounted
            pass

    def _notify_saved(self) -> None:
        if self._on_saved is not None:
            self._on_saved()

    def action_close(self) -> None:
        self.dismiss()
