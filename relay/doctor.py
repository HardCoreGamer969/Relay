"""Doctor/preflight helpers shared by the CLI and TUI."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from relay.catalog import load_catalog
from relay.client import build_client
from relay.providers import DEFAULT_PROVIDER, resolve_provider

console = Console()

def _doctor_checks(cfg, slugs) -> list[tuple[str, str, str]]:
    """The ``(label, provider, model)`` checks: ad-hoc slugs or the configured roles."""
    if slugs:
        return [("arg", DEFAULT_PROVIDER, s) for s in slugs]
    return [("brain", cfg.brain_provider, cfg.brain), ("hands", cfg.hands_provider, cfg.hands)]


def _missing_provider_keys(checks) -> list[str]:
    """Key env-vars (deduped, in order) that are needed by some provider but unset.

    v0.0.32: check both the env var AND the auth.json store -- ``build_client``
    accepts both (env > auth.json), so a key saved via ``relay config set-key``
    is just as usable as one in the env. Checking only ``os.environ`` here
    would report a false "not set" for a user who set their key in-app,
    and the doctor would hard-exit before even probing. ``resolve_key`` is
    the single source of truth for "is this provider's key present?".
    """
    from relay.secrets import resolve_key
    missing: list[str] = []
    seen: set[str] = set()
    for _, provider, _ in checks:
        if provider in seen:
            continue
        seen.add(provider)
        try:
            profile = resolve_provider(provider)
        except ValueError:
            continue  # unknown provider surfaces as a failed probe, not a key error
        if resolve_key(profile.id, profile.key_env) is None and profile.key_env not in missing:
            missing.append(profile.key_env)
    return missing


def _build_provider_clients(checks) -> dict:
    """Build one client per distinct provider; a build failure is a failed probe."""
    clients: dict = {}
    for _, provider, _ in checks:
        if provider in clients:
            continue
        try:
            clients[provider] = build_client(provider)
        except Exception as exc:  # noqa: BLE001 — surface as a failed probe, not a traceback
            console.print(f"[red]could not build the {provider} client:[/red] {exc}")
    return clients


def _safe_load_catalog():
    """Load the catalog for the status line; never let it crash doctor."""
    try:
        return load_catalog()
    except Exception:  # noqa: BLE001 — catalog status is informational only
        return None


def _probe_model(client, slug: str, provider: str = DEFAULT_PROVIDER) -> tuple[bool, str]:
    """Minimal (max_tokens=1) call to check a slug resolves. Never raises.

    Only the OpenRouter path asks for usage cost (its include flag); other
    providers get a plain probe.
    """
    try:
        extra_body = {"usage": {"include": True}} if provider == DEFAULT_PROVIDER else {}
        client.chat.completions.create(
            model=slug,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            extra_body=extra_body,
        )
        return True, "resolved"
    except Exception as exc:  # noqa: BLE001 — classify any failure as the note
        text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return False, text[:120]


def _run_doctor(checks, clients) -> tuple[list[dict], bool]:
    """Probe each ``(label, provider, slug)`` against its provider's client."""
    rows: list[dict] = []
    all_ok = True
    for label, provider, slug in checks:
        client = clients.get(provider)
        if client is None:
            ok, note = False, f"no client for provider {provider!r}"
        else:
            ok, note = _probe_model(client, slug, provider)
        rows.append(
            {"role": label, "provider": provider, "model": slug,
             "status": "OK" if ok else "FAILED", "note": note}
        )
        all_ok = all_ok and ok
    return rows, all_ok


def _print_doctor_table(rows) -> None:
    table = Table(title="Relay doctor: provider/model preflight")
    table.add_column("Role", style="bold")
    table.add_column("Provider")
    table.add_column("Model", overflow="fold")
    table.add_column("Status")
    table.add_column("Note", overflow="fold")
    for row in rows:
        style = "green" if row["status"] == "OK" else "bold red"
        table.add_row(
            row["role"], row.get("provider", ""), row["model"],
            f"[{style}]{row['status']}[/{style}]", row["note"],
        )
    console.print(table)
