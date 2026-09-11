"""Fill native API omissions without taking ownership of Hermes configuration."""

from copy import deepcopy

ROUTING_FIELDS = {
    "only": "providers_allowed",
    "ignore": "providers_ignored",
    "order": "providers_order",
    "sort": "provider_sort",
    "require_parameters": "provider_require_parameters",
    "data_collection": "provider_data_collection",
}


def session_runtime(adapter, kwargs):
    """An explicit request wins; otherwise ask Hermes to resolve its saved selection."""
    if any(
        kwargs.get(key)
        for key in (
            "requested_model",
            "requested_provider",
            "route",
            "session_model",
            "confirmed_runtime_lock",
        )
    ):
        return
    resolver = getattr(adapter, "_effective_session_runtime_request", None)
    if not callable(resolver) or not kwargs.get("session_id"):
        return
    session = adapter._ensure_session_db().get_session(kwargs["session_id"])
    if not session:
        return
    runtime = resolver(session=session, body={"model_options": kwargs.get("model_options")})
    if runtime.get("require_model_lock"):
        kwargs.update(
            route=runtime.get("route"),
            model_options=runtime.get("model_options"),
            confirmed_runtime_lock=True,
        )
    else:
        model = adapter._stored_session_model(session)
        route = adapter._resolve_route(model) if model else None
        kwargs.update(route=route, session_model=None if route else model)


def inherit_options(kwargs):
    from gateway.run import GatewayRunner

    options = kwargs.get("model_options") or {}
    if not {"service_tier", "fast"}.intersection(options):
        tier = GatewayRunner._load_service_tier()
        if tier is not None:
            kwargs["model_options"] = {**options, "service_tier": tier}


def inherit_defaults(agent):
    from gateway.run import GatewayRunner, _deep_merge_request_overrides
    from hermes_cli.models import resolve_fast_mode_overrides

    if getattr(agent, "service_tier", None) == "priority":
        fast = resolve_fast_mode_overrides(
            agent.model, provider=agent.provider, base_url=agent.base_url
        )
        agent.request_overrides = _deep_merge_request_overrides(
            agent.request_overrides or {}, fast or {}
        )
    routing = GatewayRunner._load_provider_routing()
    if not isinstance(routing, dict):
        return
    for key, attr in ROUTING_FIELDS.items():
        if key in routing and getattr(agent, attr, None) in (None, False):
            setattr(agent, attr, deepcopy(routing[key]))
