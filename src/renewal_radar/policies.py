from __future__ import annotations

import os

from .schemas import EscalationPolicy
from .store import Store


def default_escalation_policy() -> dict:
    """Preserve the original due-date reminder and environment-based escalation defaults."""
    delay = max(0, int(os.getenv("ESCALATION_AFTER_DAYS", "7")))
    return EscalationPolicy(
        reminder_days_before=[0],
        escalation_steps=[{"after_due_days": delay, "label": "Unresolved renewal"}],
    ).model_dump(mode="json")


def effective_escalation_policy(store: Store, tenant_id: str) -> dict:
    saved = store.get_tenant_policy(tenant_id)
    if saved:
        return EscalationPolicy.model_validate(saved["policy"]).model_dump(mode="json")
    return default_escalation_policy()
