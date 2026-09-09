"""Reuse Hermes's inventory and the cached effort limits its transports apply."""

import logging

log = logging.getLogger(__name__)


def model_options(refresh=False):
    from hermes_cli import inventory

    payload = inventory.build_model_options_payload(
        inventory.load_picker_context(), include_unconfigured=True, refresh=refresh
    )
    try:
        from agent.reasoning_effort import EFFORT_LADDER

        for row in payload.get("providers", []):
            reader = inventory._reasoning_catalog_reader((row.get("slug") or "").lower())
            if reader is None:
                continue
            for model, caps in row.get("capabilities", {}).items():
                if "supported_efforts" in caps:
                    continue  # Prefer the native inventory if Hermes adds this field.
                detail = reader(model)
                efforts = detail.get("supported_efforts") if isinstance(detail, dict) else None
                if isinstance(efforts, list) and efforts:
                    caps["supported_efforts"] = [
                        level for level in EFFORT_LADDER if level in efforts
                    ]
    except Exception:
        log.debug(
            "Hermes reasoning limits unavailable; using its standard inventory", exc_info=True
        )
    return payload
