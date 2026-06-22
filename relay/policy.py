"""Command-classification policy for ``bash``.

HONEST LIMITS -- read this before trusting it. This is a **best-effort speed
bump, not a security boundary.** It classifies shell command *strings* with
tokenization and pattern rules, which catches obvious accidents and the common
destructive patterns a well-meaning model emits by mistake. It does NOT defend
against an adversarial model actively trying to escape: environment-variable
expansion, command substitution, ``eval``, base64 payloads, here-docs, exotic
aliases, and intermediate pipes can all evade pattern matching. The real
boundary is process/container sandboxing -- a LATER milestone (v0.95),
explicitly NOT this one. Do not describe this layer as making bash "safe" or
"sandboxed"; it blocks obvious destructive commands and gates risky ones behind
confirmation, nothing more. Overclaiming safety breeds false confidence, which
is worse than claiming none.

Three verdicts:
  BLOCKED  -- refused outright, never run, even under auto-approve.
  CONFIRM  -- destructive but legitimately useful; pause for approval.
  ALLOW    -- everyday dev commands; run normally.

Compound commands are split on ``&&``, ``||``, ``;``, ``|`` (and subshell
parens) and each segment is classified; the command's verdict is the most
severe segment's verdict.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# --- Verdicts (ordered by severity) ----------------------------------------

BLOCKED = "BLOCKED"
CONFIRM = "CONFIRM"
ALLOW = "ALLOW"

_SEVERITY = {ALLOW: 0, CONFIRM: 1, BLOCKED: 2}


@dataclass(frozen=True)
class PolicyResult:
    """The verdict for a command plus a short, human-readable reason."""

    verdict: str
    reason: str
    command: str


# --- Rule data --------------------------------------------------------------

_PRIV_ESC = {"sudo", "su", "doas"}
_FS_DESTROY = {"fdisk", "parted", "sfdisk", "cfdisk", "mkswap", "wipefs",
               "format", "diskpart"}  # mkfs* + Windows disk tools handled separately
_POWER = {"shutdown", "reboot", "halt", "poweroff"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}

# Windows file/deletion commands (the Unix ``rm``/``rmdir`` equivalents). On Windows
# these are the destructive commands a model emits -- without them in the policy, a
# ``del /s /q *`` would be ALLOW (no confirmation) and a ``del /s /q C:\`` would
# sail past the BLOCKED guard. Matched by basename, like the Unix set.
_WIN_RM = {"del", "erase", "rmdir", "rd"}
# Windows disk-wipe / encryption-erase tool (``cipher /w:C:\`` wipes free space).
_WIN_DISK_WIPE = {"cipher"}

# A raw block device (writing to one destroys a filesystem). Character devices
# like /dev/null, /dev/zero, /dev/random are intentionally NOT matched.
_BLOCK_DEVICE_RE = re.compile(r"/dev/(?:sd|nvme|hd|vd|xvd|disk|mmcblk|sr|loop)\w*")

# Raw-text pre-checks: patterns that segmentation would tear apart, so they are
# matched against the whole command string before splitting. (These scan raw
# text and so can over-trigger on quoted literals -- a conservative bias.)
_FORK_BOMB_RE = re.compile(r"\(\s*\)\s*\{[^}]*\|[^}]*&")
_REMOTE_PIPE_SHELL_RE = re.compile(
    r"\b(?:curl|wget|fetch)\b.*\|\s*(?:sudo\s+)?(?:" + "|".join(_SHELLS) + r")\b",
    re.IGNORECASE,
)

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
# Tokens made up entirely of these chars are command separators / subshell parens.
_SEPARATOR_CHARS = set("&|;()")


# --- Helpers ----------------------------------------------------------------


def _basename(program: str) -> str:
    """Normalize a program path to its basename: ``/bin/rm`` -> ``rm``."""
    return program.replace("\\", "/").rsplit("/", 1)[-1]


def _tokenize(command: str):
    """Tokenize with shlex, returning (tokens, ok). ``ok`` is False on a parse error."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex), True
    except ValueError:
        return [], False


def _split_segments(command: str) -> list[list[str]]:
    """Split a compound command into per-segment token lists.

    Splits on ``&&``/``||``/``|``/``;`` and subshell parens. Falls back to a
    naive operator split if shlex can't parse (e.g. unbalanced quotes) -- we
    still want a (conservative) classification rather than a crash.
    """
    tokens, ok = _tokenize(command)
    if not ok:
        pieces = re.split(r"&&|\|\||[|;&()]", command)
        return [p.split() for p in pieces if p.split()]

    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= _SEPARATOR_CHARS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _is_system_or_home_path(arg: str) -> bool:
    """True if ``arg`` points at root/an absolute path/the home dir.

    Recognizes both Unix (``/``, ``~/``) and Windows (``C:\\``, ``%USERPROFILE%``)
    absolute paths, so a Windows destructive command like ``del /s /q C:\\`` is
    caught the same way ``rm -rf /`` is. Windows-style flags (``/s``, ``/q``,
    ``/f``) are NOT paths -- they are short ``/<letter>`` tokens -- so they are
    excluded to avoid false positives."""
    if arg in ("/", "/*", "/."):
        return True
    # Unix absolute paths: ``/etc``, ``/`` -- but NOT ``/s`` or ``/q`` (Windows flags,
    # a ``/`` followed by a single non-path char). A real Unix path is ``/`` + a
    # name (letters/dots), while ``/s``, ``/q``, ``/f`` are Windows command flags.
    if arg.startswith("/"):
        # A Windows-style flag: ``/`` + 1-2 letters, no slash, no dot. These are
        # NOT paths. e.g. ``/s``, ``/q``, ``/f``, ``/si``. A real Unix path like
        # ``/etc`` or ``/tmp`` has 3+ chars -- treat those as paths.
        rest = arg[1:]
        if rest and len(rest) <= 2 and rest.isalpha():
            return False
        return True
    # Windows absolute paths: ``C:\``, ``C:/``, ``\`` (drive-relative root).
    # NOTE: shlex in posix mode strips single backslashes, so ``C:\Users`` may
    # arrive as ``C:Users``. Match both the raw form (``C:\`` / ``C:/``) and the
    # shlex-stripped form (``C:`` + an uppercase word like ``C:Users``).
    if re.match(r"^[A-Za-z]:[\\/]", arg) or arg.startswith("\\"):
        return True
    # Shlex-stripped Windows path: ``C:Users`` (colon + capitalized name). The
    # capitalization distinguishes it from a Unix ``name:value`` assignment or a
    # URL scheme (``http:``). A bare drive letter ``C:`` is also a path.
    if re.match(r"^[A-Za-z]:[A-Z]", arg) or re.match(r"^[A-Za-z]:$", arg):
        return True
    if arg == "~" or arg.startswith("~/"):
        return True
    if arg in ("$HOME", "${HOME}") or arg.startswith("$HOME/") or arg.startswith("${HOME}/"):
        return True
    # Windows env-var home: %USERPROFILE% or %USERPROFILE%\...
    if arg.startswith("%USERPROFILE%"):
        return True
    return False


def _dangerous_path_target(args: list[str]) -> str | None:
    """Return the first non-flag arg that is a system/home/absolute path."""
    for a in args:
        if a.startswith("-"):
            continue
        if _is_system_or_home_path(a):
            return a
    return None


def _has_recursive_flag(args: list[str]) -> bool:
    for a in args:
        if a == "--recursive":
            return True
        if a.startswith("-") and not a.startswith("--") and "R" in a:
            return True
    return False


def _git_confirm_reason(args: list[str]) -> str | None:
    """Reason string if a git invocation is destructive enough to gate, else None."""
    force = any(a in ("--force", "-f") or a.startswith("--force-with-lease") for a in args)
    short_f = any(a.startswith("-") and not a.startswith("--") and "f" in a for a in args)
    if "push" in args and force:
        return "git push --force (rewrites remote history)"
    if "reset" in args and "--hard" in args:
        return "git reset --hard (discards working-tree changes)"
    if "clean" in args and (short_f or "--force" in args):
        return "git clean -f (deletes untracked files)"
    return None


def _is_sigkill(args: list[str]) -> bool:
    for i, a in enumerate(args):
        if a in ("-9", "-KILL", "-SIGKILL"):
            return True
        if a == "-s" and i + 1 < len(args) and args[i + 1] in ("9", "KILL", "SIGKILL"):
            return True
    return False


def _classify_segment(tokens: list[str], command: str) -> PolicyResult | None:
    """Classify one command segment. Returns None for an empty/assignment-only segment."""
    # Skip leading ``NAME=value`` environment assignments to find the real program.
    i = 0
    while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
        i += 1
    if i >= len(tokens):
        return None

    rest = tokens[i:]
    prog = _basename(rest[0])
    args = rest[1:]

    # ---- BLOCKED ----------------------------------------------------------
    if prog in _PRIV_ESC:
        return PolicyResult(BLOCKED, f"privilege escalation ({prog})", command)
    if prog.startswith("mkfs") or prog in _FS_DESTROY:
        return PolicyResult(BLOCKED, f"filesystem/partition destruction ({prog})", command)
    if prog in _POWER:
        return PolicyResult(BLOCKED, f"changes system power state ({prog})", command)
    for tok in rest:  # redirects/dd args anywhere in the segment
        if _BLOCK_DEVICE_RE.search(tok):
            return PolicyResult(BLOCKED, f"targets a raw block device ({tok})", command)
    if prog == "rm":
        target = _dangerous_path_target(args)
        if target is not None:
            return PolicyResult(BLOCKED, f"rm on a system/home/absolute path ({target})", command)
    # Windows deletion commands (del/erase/rmdir/rd): same BLOCKED/CONFIRM logic as
    # rm. A system/home/absolute path -> BLOCKED; an in-project deletion -> CONFIRM.
    # Without this, ``del /s /q *`` would be ALLOW (no confirmation) on Windows.
    if prog in _WIN_RM:
        target = _dangerous_path_target(args)
        if target is not None:
            return PolicyResult(BLOCKED, f"{prog} on a system/home/absolute path ({target})", command)
    # Windows disk wipe (cipher /w:...) -- always BLOCKED (wipes free space).
    if prog in _WIN_DISK_WIPE:
        return PolicyResult(BLOCKED, f"disk wipe tool ({prog})", command)
    if prog in ("chmod", "chown") and _has_recursive_flag(args):
        target = _dangerous_path_target(args)
        if target is not None:
            return PolicyResult(
                BLOCKED, f"recursive {prog} on a system/absolute path ({target})", command
            )

    # ---- CONFIRM ----------------------------------------------------------
    if prog == "rm":
        # Not blocked above => an in-project deletion. Gate it: rm is destructive
        # and not an "everyday safe" command, so bias to CONFIRM over ALLOW.
        return PolicyResult(CONFIRM, "deletes files (rm)", command)
    if prog in _WIN_RM:
        # Not blocked above => an in-project deletion. Same rationale as rm: gate it.
        return PolicyResult(CONFIRM, f"deletes files ({prog})", command)
    if prog == "git":
        reason = _git_confirm_reason(args)
        if reason is not None:
            return PolicyResult(CONFIRM, reason, command)
    if prog in ("chmod", "chown") and _has_recursive_flag(args):
        return PolicyResult(CONFIRM, f"recursive {prog} inside the project", command)
    if prog == "kill" and _is_sigkill(args):
        return PolicyResult(CONFIRM, "force-kills a process (kill -9)", command)
    if prog in ("pkill", "killall"):
        return PolicyResult(CONFIRM, f"kills processes by name ({prog})", command)

    # ---- ALLOW ------------------------------------------------------------
    return PolicyResult(ALLOW, "ordinary command", command)


def classify(command: str) -> PolicyResult:
    """Classify a shell command into ``BLOCKED`` / ``CONFIRM`` / ``ALLOW``.

    The verdict is the most severe verdict across the command's segments; any
    ``BLOCKED`` segment makes the whole command ``BLOCKED``.
    """
    cmd = command.strip()
    if not cmd:
        return PolicyResult(ALLOW, "empty command", command)

    # Raw-text pre-checks (segmentation would split these apart).
    if _FORK_BOMB_RE.search(cmd):
        return PolicyResult(BLOCKED, "fork bomb pattern", command)
    if _REMOTE_PIPE_SHELL_RE.search(cmd):
        return PolicyResult(BLOCKED, "pipes remote content directly into a shell", command)

    worst = PolicyResult(ALLOW, "ordinary command", command)
    for tokens in _split_segments(cmd):
        result = _classify_segment(tokens, command)
        if result is None:
            continue
        if _SEVERITY[result.verdict] > _SEVERITY[worst.verdict]:
            worst = result
            if worst.verdict == BLOCKED:
                break
    return worst
