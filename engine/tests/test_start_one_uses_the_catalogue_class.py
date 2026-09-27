"""Start one must not look up a column the schema never created.

A workflow the customer made has no work class on the row. The catalogue knows the
class of the twelve shipped workflows. Reading workflow_definitions.work_class raised
undefined-column, and the click ended as a 500.
"""

from boss_engine.services.workflow_samples import catalogue_work_class


def test_a_shipped_workflow_keeps_the_class_it_was_written_for():
    assert catalogue_work_class("standard_product_delivery") == "project.new"
    assert catalogue_work_class("fast_fix") == "bugfix"


def test_a_workflow_the_customer_made_has_no_class():
    assert catalogue_work_class("needs_a_video") == ""
    assert catalogue_work_class("") == ""
    assert catalogue_work_class("  standard_product_delivery  ") == "project.new"


def test_start_does_not_select_a_work_class_column():
    from pathlib import Path

    source = Path(__file__).resolve().parents[2].joinpath(
        "apps/api/app/main.py"
    ).read_text()
    assert "SELECT work_class FROM workflow_definitions" not in source
