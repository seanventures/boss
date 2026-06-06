from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "kubernetes" / "base"
HELM = ROOT / "deploy" / "helm" / "boss"


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


def _contains_open_namespace_selector(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("namespaceSelector") == {}:
            return True
        return any(_contains_open_namespace_selector(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_open_namespace_selector(item) for item in value)
    return False


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

    network_policy = by_key[("NetworkPolicy", "boss-control-plane-ingress")]
    if _contains_open_namespace_selector(network_policy.get("spec", {}).get("ingress")):
        raise ManifestValidationError(
            "Kubernetes base NetworkPolicy must not allow ingress from every namespace."
        )

    return resources


def validate_helm_chart() -> dict[str, Any]:
    chart = yaml.safe_load((HELM / "Chart.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((HELM / "values.yaml").read_text(encoding="utf-8"))
    if chart.get("name") != "boss":
        raise ManifestValidationError("Helm chart must be named boss.")
    if chart.get("type") != "application":
        raise ManifestValidationError("Helm chart type must be application.")
    if (values.get("auth") or {}).get("requireApiAuth") is not True:
        raise ManifestValidationError("Helm chart must enable API auth by default.")
    if (values.get("secrets") or {}).get("create") is not False:
        raise ManifestValidationError(
            "Helm chart must not create placeholder secrets by default."
        )
    safety = values.get("safety") or {}
    for key in (
        "infraRealProvisionAllowed",
        "emailRealSendAllowed",
        "slackRealSendAllowed",
        "sandboxMergeEnabled",
        "sandboxDeployEnabled",
    ):
        if safety.get(key) is not False:
            raise ManifestValidationError(f"Helm safety default must keep {key} false.")

    templates = HELM / "templates"
    required_templates = {
        "_helpers.tpl",
        "configmap.yaml",
        "secret.yaml",
        "serviceaccount.yaml",
        "artifacts-pvc.yaml",
        "api.yaml",
        "web.yaml",
        "worker.yaml",
        "bootstrap-job.yaml",
        "networkpolicy.yaml",
    }
    present = {path.name for path in templates.iterdir() if path.is_file()}
    missing = sorted(required_templates - present)
    if missing:
        raise ManifestValidationError(f"Helm chart missing templates: {missing}")

    required_fragments = {
        "api.yaml": ["kind: Deployment", "HorizontalPodAutoscaler", "PodDisruptionBudget"],
        "web.yaml": ["kind: Deployment", "HorizontalPodAutoscaler", "PodDisruptionBudget"],
        "worker.yaml": [
            "kind: Deployment",
            "BOSS_WORKER_API_KEY",
            "HorizontalPodAutoscaler",
            "PodDisruptionBudget",
        ],
        "bootstrap-job.yaml": ["kind: Job", "apps.api.app.bootstrap"],
        "secret.yaml": ["if .Values.secrets.create", "kind: Secret"],
    }
    for template_name, fragments in required_fragments.items():
        text_payload = (templates / template_name).read_text(encoding="utf-8")
        for fragment in fragments:
            if fragment not in text_payload:
                raise ManifestValidationError(
                    f"Helm template {template_name} missing required fragment: {fragment}"
                )

    network_policy_text = (templates / "networkpolicy.yaml").read_text(encoding="utf-8")
    if "namespaceSelector: {}" in network_policy_text:
        raise ManifestValidationError(
            "Helm NetworkPolicy must not allow ingress from every namespace."
        )

    return {"chart": chart, "values": values, "templates": sorted(present)}


def main() -> None:
    resources = validate_kubernetes_base()
    helm = validate_helm_chart()
    print(
        f"Validated {len(resources)} Kubernetes resources from {BASE} "
        f"and Helm chart {helm['chart']['version']} from {HELM}."
    )


if __name__ == "__main__":
    main()
