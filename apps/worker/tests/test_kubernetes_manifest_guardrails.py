from scripts.validate_kubernetes_manifests import (
    validate_helm_chart,
    validate_kubernetes_base,
)


def test_kubernetes_network_policy_does_not_allow_all_namespaces():
    resources = validate_kubernetes_base()
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


def test_helm_network_policy_template_does_not_allow_all_namespaces():
    chart = validate_helm_chart()

    assert "networkpolicy.yaml" in chart["templates"]
