from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "kubernetes" / "base"


class ManifestValidationError(RuntimeError):
    pass


def _load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    documents = []
    for item in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if item is None:
            continue
        if not isinstance(item, dict):
            raise ManifestValidationError(f"{path} contains a non-object document.")
        documents.append(item)
    return documents


def _resource_key(resource: dict[str, Any]) -> tuple[str, str]:
    metadata = resource.get("metadata") or {}
    return str(resource.get("kind")), str(metadata.get("name"))


def load_kubernetes_base() -> list[dict[str, Any]]:
    kustomization = yaml.safe_load(
        (BASE / "kustomization.yaml").read_text(encoding="utf-8")
    )
    resources = kustomization.get("resources")
    if not isinstance(resources, list) or not resources:
        raise ManifestValidationError("kustomization.yaml must list resources.")
    if "secret-template.yaml" in resources:
        raise ManifestValidationError(
            "secret-template.yaml must not be applied by default."
        )

    loaded: list[dict[str, Any]] = []
    for resource in resources:
        path = BASE / resource
        if not path.exists():
            raise ManifestValidationError(
                f"kustomization resource does not exist: {resource}"
            )
        loaded.extend(_load_yaml_documents(path))
    return loaded


def validate_kubernetes_base() -> list[dict[str, Any]]:
    resources = load_kubernetes_base()
    by_key = {_resource_key(resource): resource for resource in resources}
    required = {
        ("Namespace", "boss"),
        ("ServiceAccount", "boss-control-plane"),
        ("ConfigMap", "boss-config"),
        ("PersistentVolumeClaim", "boss-artifacts"),
        ("Deployment", "boss-api"),
        ("Deployment", "boss-web"),
        ("Deployment", "boss-worker"),
        ("Service", "boss-api"),
        ("Service", "boss-web"),
        ("Job", "boss-bootstrap-seed"),
        ("NetworkPolicy", "boss-control-plane-ingress"),
        ("HorizontalPodAutoscaler", "boss-api"),
        ("HorizontalPodAutoscaler", "boss-web"),
        ("HorizontalPodAutoscaler", "boss-worker"),
        ("PodDisruptionBudget", "boss-api"),
        ("PodDisruptionBudget", "boss-web"),
        ("PodDisruptionBudget", "boss-worker"),
    }
    missing = sorted(required - set(by_key))
    if missing:
        raise ManifestValidationError(f"missing required resources: {missing}")

    for name in ("boss-api", "boss-web"):
        deployment = by_key[("Deployment", name)]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        if "readinessProbe" not in container or "livenessProbe" not in container:
            raise ManifestValidationError(
                f"{name} must define readiness and liveness probes."
            )
        if not any("secretRef" in item for item in container.get("envFrom", [])):
            raise ManifestValidationError(
                f"{name} must read runtime secrets from boss-secrets."
            )

    worker = by_key[("Deployment", "boss-worker")]
    worker_container = worker["spec"]["template"]["spec"]["containers"][0]
    if not any("secretRef" in item for item in worker_container.get("envFrom", [])):
        raise ManifestValidationError(
            "boss-worker must read runtime secrets from boss-secrets."
        )
    if not any(
        mount.get("mountPath") == "/var/lib/boss/artifacts"
        for mount in worker_container.get("volumeMounts", [])
    ):
        raise ManifestValidationError(
            "boss-worker must mount the shared artifacts volume."
        )

    config = by_key[("ConfigMap", "boss-config")]
    data = config.get("data") or {}
    if data.get("BOSS_REQUIRE_API_AUTH") != "true":
        raise ManifestValidationError("Kubernetes base must enable API auth by default.")
    if data.get("BOSS_INFRA_REAL_PROVISION_ALLOWED") != "false":
        raise ManifestValidationError(
            "Kubernetes base must default provider writes off."
        )

    secret_template = _load_yaml_documents(BASE / "secret-template.yaml")[0]
    if secret_template.get("kind") != "Secret":
        raise ManifestValidationError("secret-template.yaml must be a Secret.")
    if secret_template.get("metadata", {}).get("name") != "boss-secrets":
        raise ManifestValidationError("secret-template.yaml must define boss-secrets.")
    for key in ("DATABASE_URL", "BOSS_API_KEYS", "BOSS_API_KEY", "BOSS_WORKER_API_KEY"):
        if key not in (secret_template.get("stringData") or {}):
            raise ManifestValidationError(f"secret-template.yaml missing {key}.")

    return resources


def main() -> None:
    resources = validate_kubernetes_base()
    print(f"Validated {len(resources)} Kubernetes resources from {BASE}.")


if __name__ == "__main__":
    main()
