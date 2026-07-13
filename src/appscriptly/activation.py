"""Unified activation-UX contract for generated Apps Script automations.

Shared by the ``as_*`` installer families that end a deploy with an
ACTIVATION WALL - a manual step the user must take in the Apps Script
editor before the automation does anything:

  * Classes D/E (time-driven + reactive triggers): an installable trigger
    only EXISTS after ``installTrigger`` is run once.
  * Class F (on-demand grade / refresh): the action only happens when its
    function is run once.
  * Class G (slides-to-video render): frames only exist after
    ``renderFrames`` is run once.
  * Class H (standalone web app): Google's per-script consent door serves
    the ``/exec`` URL until the deploying user runs any function once.

Before this module each family hand-rolled similar-but-different fields
(``trigger_active`` / ``run_required`` / ``activation_note`` / a bare
``activation_instructions``). ``build_activation_fields`` gives every
family ONE canonical shape so a client can treat "this needs one manual
step" uniformly, while each family keeps its own legacy field(s) as
back-compatible aliases:

    activation_required     (bool) - True while a manual Run + Allow remains
    activation_function     (str)  - the EXACT function the user runs
    activation_url          (str)  - the script-editor deep link (see below)
    activation_instructions (str)  - the literal one-step remediation

DEEP-LINK REALITY (researched 2026-07-13, driver S0-6). The Apps Script
editor exposes NO documented URL that lands on a specific function or
pre-selects the Run control - confirmed across Google's own developer
docs, the current-IDE release notes, and the community feature guides;
nothing better than the editor ROOT exists. So ``activation_url`` is that
root (``https://script.google.com/d/{scriptId}/edit``, which opens the
project with its first file selected) and the SMOOTHING lives in the
instructions: they name the exact function and the
function-dropdown -> Run -> Allow path, because a URL cannot.
"""
from __future__ import annotations

# The editor-root deep link. This is the closest an Apps Script URL gets
# to the activation target - no function-select / Run-dropdown URL
# parameter exists (see module docstring), so callers pair it with
# instructions that name the exact function.
ACTIVATION_EDITOR_URL_TEMPLATE = "https://script.google.com/d/{script_id}/edit"


def activation_editor_url(script_id: str) -> str:
    """Return the script-editor deep link for ``script_id`` (the editor root)."""
    return ACTIVATION_EDITOR_URL_TEMPLATE.format(script_id=script_id)


def build_activation_fields(
    script_id: str,
    activation_function: str,
    activation_instructions: str,
) -> dict:
    """Build the canonical activation payload shared across the as_* families.

    Returns the four unified keys. ``activation_required`` is always True:
    this builder is for the case where a manual Run + Allow still remains
    (the state every fresh install/deploy is in). Callers merge the result
    into their return dict and keep any legacy alias field (for example
    ``trigger_active``, ``run_required``, ``activation_note``) alongside so
    existing consumers do not break.

    Args:
        script_id: the generated project's scriptId; ``activation_url`` is
            derived from it.
        activation_function: the exact function the user runs once in the
            editor to activate the automation (e.g. ``installTrigger``,
            ``renderFrames``, ``gradeResponses``).
        activation_instructions: the literal one-step remediation string.
            Must name ``activation_function`` and the Run + Allow path so
            the user can act without a function-level deep link.
    """
    return {
        "activation_required": True,
        "activation_function": activation_function,
        "activation_url": activation_editor_url(script_id),
        "activation_instructions": activation_instructions,
    }
