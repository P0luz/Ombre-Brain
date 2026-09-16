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
    ("deployment_profile", "ombrebrain.security.deployment_profile"),
    ("ledger_replay", "ombrebrain.eventsourcing.ledger_replay"),
    ("ledger_mirror", "ombrebrain.eventsourcing.ledger_mirror"),
    ("projection_mirror", "ombrebrain.projection.projection_mirror"),
    ("projection_sqlite", "ombrebrain.projection.projection_sqlite"),
    ("projection_vector", "ombrebrain.projection.projection_vector"),
)


ACTIVE_FLAT_MODULES = (
    # These names are not compatibility shims.  They implement the isolated
    # M-04 archive and M-05 outbox protocols; the package modules implement
    # the separate production backup and write-behind queue contracts.
    ("backup_archive", "ombrebrain.storage.backup_archive"),
    ("embedding_outbox", "ombrebrain.storage.embedding_outbox"),
)


@pytest.mark.parametrize(("legacy_name", "canonical_name"), RETIRED_MODULES)
def test_flat_module_is_retired_and_canonical_module_imports(
    legacy_name: str,
    canonical_name: str,
) -> None:
    assert importlib.util.find_spec(legacy_name) is None
    assert importlib.import_module(canonical_name).__name__ == canonical_name


@pytest.mark.parametrize(("flat_name", "package_name"), ACTIVE_FLAT_MODULES)
def test_active_flat_protocol_module_is_distinct_from_package_module(
    flat_name: str,
    package_name: str,
) -> None:
    flat = importlib.import_module(flat_name)
    package = importlib.import_module(package_name)

    assert flat.__name__ == flat_name
    assert package.__name__ == package_name
    assert flat.__file__ != package.__file__
