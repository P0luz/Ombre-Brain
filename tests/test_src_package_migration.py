"""Regression checks for retired flat ``src`` compatibility modules."""

import importlib
import importlib.util

import pytest


RETIRED_MODULES = (
    ("memory_messages", "ombrebrain.domain.memory_messages"),
    ("plan_history", "ombrebrain.domain.plan_history"),
    ("provider_detect", "ombrebrain.integrations.provider_detect"),
    ("public_origin", "ombrebrain.security.public_origin"),
    ("bucket_scoring", "ombrebrain.retrieval.bucket_scoring"),
    ("media_store", "ombrebrain.storage.media_store"),
    ("vault_health", "ombrebrain.storage.vault_health"),
    ("backup_archive", "ombrebrain.storage.backup_archive"),
    ("embedding_outbox", "ombrebrain.storage.embedding_outbox"),
    ("deployment_profile", "ombrebrain.security.deployment_profile"),
    ("ledger_replay", "ombrebrain.eventsourcing.ledger_replay"),
    ("ledger_mirror", "ombrebrain.eventsourcing.ledger_mirror"),
    ("projection_mirror", "ombrebrain.projection.projection_mirror"),
    ("projection_sqlite", "ombrebrain.projection.projection_sqlite"),
    ("projection_vector", "ombrebrain.projection.projection_vector"),
)


@pytest.mark.parametrize(("legacy_name", "canonical_name"), RETIRED_MODULES)
def test_flat_module_is_retired_and_canonical_module_imports(
    legacy_name: str,
    canonical_name: str,
) -> None:
    assert importlib.util.find_spec(legacy_name) is None
    assert importlib.import_module(canonical_name).__name__ == canonical_name
