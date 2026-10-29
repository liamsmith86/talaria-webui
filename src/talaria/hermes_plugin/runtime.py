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
    saved = saved_runtime(adapter, kwargs.get("session_id"), kwargs.get("model_options"))
    if saved is None:
        return
    runtime, model = saved
    if runtime.get("require_model_lock"):
        kwargs.update(
            route=runtime.get("route"),
            model_options=runtime.get("model_options"),
            confirmed_runtime_lock=True,
        )
    else:
        route = adapter._resolve_route(model) if model else None
        kwargs.update(route=route, session_model=None if route else model)


def saved_runtime(adapter, session_id, model_options, db=None):
    """Hermes's resolution of a session's saved selection, and its stored model."""
    resolver = getattr(adapter, "_effective_session_runtime_request", None)
    if not callable(resolver) or not session_id:
        return None
    session = (db or adapter._ensure_session_db()).get_session(session_id)
    if not session:
        return None
    runtime = resolver(session=session, body={"model_options": model_options})
    return runtime, adapter._stored_session_model(session)


def authority_runs(adapter):
    """Hermes 0.22.1+ runs admitted API turns itself; no agent passes through Talaria."""
    return getattr(getattr(adapter, "gateway_runner", None), "session_authority", None) is not None


def session_request(adapter, db, body, instructions, use_default_model):
    """Request what the agent path would inherit, using fields Hermes's own turns read.

    Hermes applies the profile's prefill, service tier, routing and fast mode itself.
    """
    body = dict(body)
    if instructions and not body.get("instructions"):
        body["instructions"] = instructions
    if use_default_model or body.get("model") or body.get("provider"):
        return body
    saved = saved_runtime(adapter, body.get("session_id"), body.get("model_options"), db)
    if saved is None:
        return body
    runtime, model = saved
    if runtime.get("require_model_lock"):
        # These runs do not read a confirmed lock themselves; request its selection.
        requested = runtime.get("requested") or {}
        model = requested.get("model")
        if requested.get("provider"):
            body["provider"] = requested["provider"]
        if runtime.get("model_options") and "model_options" not in body:
            body["model_options"] = runtime["model_options"]
    if model:
        body["model"] = model
    return body


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
