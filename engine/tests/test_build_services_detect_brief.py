"""A new app's brief naming PostgreSQL gets a build database (HelpDesk crun_b4613dcf got none)."""
from boss_engine.services import build_services


def test_the_operators_brief_in_instructions_names_postgres():
    spec = {"instructions": "Build HelpDesk AI. Express backend, PostgreSQL 16 through a typed "
                            "query layer.\n\n---\nspec follows"}
    assert build_services.decide((), None, spec=spec, title="HelpDesk")["services"] == ("postgres",)


def test_template_material_alone_does_not_ask_for_a_database():
    spec = {"instructions": "Add a hello endpoint.\n\n<<<BEGIN UNTRUSTED TEMPLATE MATERIAL>>>\n"
                            "Works with PostgreSQL or MySQL.\n"}
    assert build_services.decide((), None, spec=spec, title="hello")["services"] == ()
