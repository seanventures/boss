from scripts.validate_kubernetes_manifests import (
    validate_helm_chart,
    validate_kubernetes_base,
)


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
    network_policy = next(
        resource
        for resource in resources
        if resource.get("kind") == "NetworkPolicy"
        and resource["metadata"]["name"] == "boss-control-plane-ingress"
    )
    ingress_sources = [
        source
        for rule in network_policy["spec"].get("ingress", [])
        for source in rule.get("from", [])
    ]
    assert {"namespaceSelector": {}} not in ingress_sources


def test_helm_chart_keeps_enterprise_self_hosted_defaults_fail_closed():
    chart = validate_helm_chart()
    values = chart["values"]

    assert values["auth"]["requireApiAuth"] is True
    assert values["secrets"]["create"] is False
    assert values["safety"]["infraRealProvisionAllowed"] is False
    assert values["safety"]["sandboxDeployEnabled"] is False
    assert {"api.yaml", "web.yaml", "worker.yaml"} <= set(chart["templates"])
