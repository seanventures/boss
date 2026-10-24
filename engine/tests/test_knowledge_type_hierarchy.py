"""Each parser records what a type extends and implements (the code graph's type hierarchy).

BOSS's agent was asked which classes implement a loan repayment schedule transaction processor
(Apache Fineract) and could only match names, because the code graph had no extends/implements
edges: it found a sample class and called it the only one. These are the parse-level facts the
edges are built from, one small fixture per language, and the cache shape that carries them.
"""
from __future__ import annotations

import pytest

from boss_engine.services.knowledge.code_index import facts_from_dict, facts_to_row, names_of, row_to_facts
from boss_engine.services.knowledge.code_parse import FACTS_VERSION, parse_file, type_chain


def _bases(path: str, source: str) -> set[tuple[str, tuple[str, ...], str]]:
    facts = parse_file(path, source.encode())
    assert facts.error is None, facts.error
    return {(src, chain, rel) for src, chain, rel, _line in facts.bases}


def test_java_superclass_interfaces_interface_parents_enums_records_and_nested_types():
    source = """
        package org.bank.loan;
        import org.bank.core.Strategy;
        public abstract class AbstractProcessor<T> extends org.bank.core.Base<T> implements Strategy, Named<T> {
            static class Inner extends AbstractProcessor<String> { }
        }
        interface Processor extends Strategy, java.io.Serializable { }
        enum Mode implements Named<Mode> { A }
        record Pair(int a) implements Strategy { }
        class Plain { }
    """
    facts = parse_file("src/AbstractProcessor.java", source.encode())
    assert {(s, c, r) for s, c, r, _ in facts.bases} == {
        ("AbstractProcessor", ("org", "bank", "core", "Base"), "extends"),
        ("AbstractProcessor", ("Strategy",), "implements"),
        ("AbstractProcessor", ("Named",), "implements"),
        ("AbstractProcessor.Inner", ("AbstractProcessor",), "extends"),
        ("Processor", ("Strategy",), "extends"),
        ("Processor", ("java", "io", "Serializable"), "extends"),
        ("Mode", ("Named",), "implements"),
        ("Pair", ("Strategy",), "implements"),
    }
    assert facts.types == {"AbstractProcessor": "class", "AbstractProcessor.Inner": "class",
                           "Processor": "interface", "Mode": "enum", "Pair": "record", "Plain": "class"}


def test_typescript_and_javascript_extends_and_implements():
    ts = """
        class Repo extends Base<Order> implements Store, ns.Named { }
        abstract class Cached extends Repo { }
        interface Store extends Reader, Writer<Order> { }
        class Mixed extends mixin(Base) { }
    """
    assert _bases("src/repo.ts", ts) == {
        ("Repo", ("Base",), "extends"), ("Repo", ("Store",), "implements"),
        ("Repo", ("ns", "Named"), "implements"), ("Cached", ("Repo",), "extends"),
        ("Store", ("Reader",), "extends"), ("Store", ("Writer",), "extends"),
    }
    assert parse_file("src/repo.ts", ts.encode()).types["Store"] == "interface"
    js = "class A extends B {}\nclass C extends lib.D {}\n"
    assert _bases("src/a.js", js) == {("A", ("B",), "extends"), ("C", ("lib", "D"), "extends")}


def test_python_bases_skip_object_and_generic_and_keep_subscripted_bases():
    source = (
        "import typing\n"
        "class Repo(Base, models.Model, typing.Generic[T], object, metaclass=Meta):\n    pass\n"
        "class Rows(List[int]):\n    pass\n"
    )
    assert _bases("app/repo.py", source) == {
        ("Repo", ("Base",), "extends"), ("Repo", ("models", "Model"), "extends"), ("Rows", ("List",), "extends")}


def test_csharp_bases_leave_class_or_interface_to_the_index_except_an_interfaces_parents():
    source = "namespace Bank;\nclass Repo : Base, IStore<Order> { }\ninterface IStore : IReader { }\nstruct S : IReader { }\n"
    assert _bases("Repo.cs", source) == {
        ("Repo", ("Base",), "inherits"), ("Repo", ("IStore",), "inherits"),
        ("IStore", ("IReader",), "extends"), ("S", ("IReader",), "inherits"),
    }


def test_go_embedding_in_structs_and_interfaces():
    source = "package bank\ntype Repo struct {\n\tBase\n\t*store.Cache\n\tname string\n}\ntype RW interface {\n\tReader\n\tWrite() error\n}\n"
    assert _bases("bank/repo.go", source) == {
        ("Repo", ("Base",), "extends"), ("Repo", ("store", "Cache"), "extends"), ("RW", ("Reader",), "extends")}
    assert parse_file("bank/repo.go", source.encode()).types == {"Repo": "struct", "RW": "interface"}


@pytest.mark.parametrize(("raw", "chain"), [
    ("pkg.Base<T>", ("pkg", "Base")), ("Map<K, List<V>>", ("Map",)), ("*store.Cache", ("store", "Cache")),
    ("global::Bank.Base", ("Bank", "Base")), ("mixin(Base)", None), ("", None)])
def test_a_base_written_in_source_becomes_a_name_chain(raw, chain):
    assert type_chain(raw) == chain


def test_cached_facts_carry_bases_and_their_version_and_older_cache_reads_as_version_one():
    facts = parse_file("src/A.java", b"package p; class A extends B implements C { }")
    row = facts_to_row(facts)
    back = row_to_facts(facts.path, facts.lang, facts.module, facts.content_hash, row)
    assert back.bases == facts.bases and back.types == facts.types and back.version == FACTS_VERSION
    assert {"B", "C"} <= set(names_of(back))
    old = {k: v for k, v in row.items() if k not in ("bases", "types", "v")}
    stale = facts_from_dict({"path": "src/A.java", "lang": "java", "module": "p", "content_hash": "x", **old})
    assert stale.version == 1 and stale.bases == []
