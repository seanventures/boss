"""AWS EC2 bare-metal instance type helpers.

Bare metal on AWS is not a separate API: use the same
``aws.ec2.instance.create`` skill with an instance type whose name ends in
``.metal`` (classic) or contains a ``metal-`` segment (newer families such as
``u-6tb1.metal`` / ``mac2-m2.metal``). resource_plane already provisions EC2;
this module only classifies and recommends metal SKUs so catalogs/UI can
surface bare-metal clearly without rewriting the adapter.
"""

from __future__ import annotations

from typing import Optional

# Well-known metal instance types (not exhaustive; AWS adds SKUs over time).
# Prefer current-generation general/compute/memory families for recommendations.
_RECOMMENDED_METAL_BY_REGION: dict[str, list[str]] = {
    # Default global shortlist — widely available in commercial regions.
    "*": [
        "m5.metal",
        "m6i.metal",
        "c5.metal",
        "c6i.metal",
        "r5.metal",
        "r6i.metal",
        "i3.metal",
        "z1d.metal",
    ],
    "us-east-1": [
        "m5.metal",
        "m6i.metal",
        "c5.metal",
        "c6i.metal",
        "r5.metal",
        "r6i.metal",
        "i3.metal",
        "i4i.metal",
        "z1d.metal",
        "u-6tb1.metal",
    ],
    "us-west-2": [
        "m5.metal",
        "m6i.metal",
        "c5.metal",
        "c6i.metal",
        "r5.metal",
        "r6i.metal",
        "i3.metal",
        "z1d.metal",
    ],
    "eu-west-1": [
        "m5.metal",
        "m6i.metal",
        "c5.metal",
        "c6i.metal",
        "r5.metal",
        "r6i.metal",
        "i3.metal",
    ],
}


def is_bare_metal_instance_type(instance_type: str) -> bool:
    """Return True if *instance_type* denotes an AWS EC2 bare-metal SKU.

    Matches:
      * suffix ``.metal`` (e.g. ``m5.metal``, ``c6i.metal``)
      * embedded ``metal-`` segment (e.g. some specialized / future families)
    """
    name = (instance_type or "").strip().lower()
    if not name:
        return False
    if name.endswith(".metal"):
        return True
    if "metal-" in name:
        return True
    # Bare name "metal" is not a valid type; require structure above.
    return False


def recommend_metal_types(region_hint: Optional[str] = None) -> list[str]:
    """Return a short list of commonly available metal instance types.

    *region_hint* narrows to a region-specific shortlist when known; otherwise
    the global default list is returned. This is advisory only — callers should
    still validate availability via EC2 describe-instance-type-offerings.
    """
    region = (region_hint or "").strip().lower() or None
    if region and region in _RECOMMENDED_METAL_BY_REGION:
        return list(_RECOMMENDED_METAL_BY_REGION[region])
    return list(_RECOMMENDED_METAL_BY_REGION["*"])
