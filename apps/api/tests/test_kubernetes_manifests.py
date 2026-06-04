from scripts.validate_kubernetes_manifests import validate_kubernetes_base


def test_kubernetes_base_contains_enterprise_ha_workloads_and_guardrails():
    resources = validate_kubernetes_base()

    deployment_names = {
        resource["metadata"]["name"]
        for resource in resources
        if resource.get("kind") == "Deployment"
    }
    hpa_names = {
        resource["metadata"]["name"]
        for resource in resources
        if resource.get("kind") == "HorizontalPodAutoscaler"
    }

    assert {"boss-api", "boss-web", "boss-worker"} <= deployment_names
    assert {"boss-api", "boss-web", "boss-worker"} <= hpa_names
