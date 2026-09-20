"""Hermes loads this optional plugin through its ordinary enable/disable gate."""

from .recorder import record_event
from . import auxiliary


def register(ctx):
    """Resolve the owning profile per callback, including timeout worker contexts."""
    def callback(kind):
        def observe(**kwargs):
            from hermes_constants import get_hermes_home
            # CLI exit can emit on_session_end without an actual native turn.
            if kind == "turn_end" and not kwargs.get("turn_id"):
                return
            record_event(get_hermes_home(), kind, kwargs)
        return observe

    for hook, kind in (
        ("pre_llm_call", "turn_start"), ("pre_api_request", "request_start"),
        ("post_api_request", "request_end"), ("api_request_error", "request_error"),
        ("on_session_end", "turn_end"),
    ):
        ctx.register_hook(hook, callback(kind))

    # Old Hermes hosts keep the existing main-loop recorder. Register only when
    # this additive capability is advertised; unknown hook names otherwise warn.
    from hermes_cli.plugins import VALID_HOOKS
    if "on_aux_usage" in VALID_HOOKS:
        def observe_aux(**kwargs):
            from hermes_constants import get_hermes_home
            auxiliary.record_aux_event(get_hermes_home(), kwargs)
        ctx.register_hook("on_aux_usage", observe_aux)
