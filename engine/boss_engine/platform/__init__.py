"""BOSS Infrastructure Platform: capability requests, plugins, plans, bindings and evidence.

`contract` is the stable `boss.plugin/v1` surface every provider plugin builds against.
"""
from .contract import (  # noqa: F401
    API_VERSION, CONTRACT_VERSION, HOOKS, MUTATING_HOOKS, READ_ONLY_HOOKS, REQUIRED_HOOKS,
    SUCCESS_STATUS, AttachSpec, ContractError, CostEstimate, Evidence, Hook, HookContext,
    InfraPlugin, OperationError, OperationResult, OperationStatus, PlannedChange, PluginManifest,
    PluginPermissions, PluginRuntime, PluginSource, PortSpec, ResourceMode, ResourcePlan,
    ResourceRequest, SecretBroker, SecretRef, SupportLevel, digest_of, idempotency_key,
    load_manifest, validate_manifest, validate_resources,
)
