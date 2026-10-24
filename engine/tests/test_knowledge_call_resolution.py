"""A call is resolved by its receiver's declared type, through the type hierarchy.

Recorded session, 2026-10-24: asked what calls into Fineract's due/in-advance strategy's
repayment allocation, and who uses ``determineProcessor``, BOSS's agent got 0 ``calls`` edges
while code search found the callers: a bare call on ``this`` to an abstract method, a
constructor-injected field (``private final Factory factory; factory.determineProcessor()``),
a factory-returned local and an interface-typed receiver all got no edge. The graph now types
the receiver (field, parameter, local, ``this``, a call's declared return type, ``new T()``):
``calls`` lands on the method that type has, its own or inherited, and ``dispatches_to`` on
each override in its subtypes. One fixture per language, plus the cases that must NOT link.
"""
from __future__ import annotations

import pytest

from boss_engine.services.knowledge.code_index import (
    DISPATCH_CONFIDENCE, RepoIndex, facts_to_row, graph_for, names_of, row_to_facts)
from boss_engine.services.knowledge.code_parse import CALL, FACTS_VERSION, parse_file
from boss_engine.services.knowledge.store import identifier_terms

pytest.importorskip("tree_sitter_java")


def _graph(files: dict[str, str]) -> dict[tuple[str, str, str], float]:
    facts = [parse_file(path, body.encode()) for path, body in files.items()]
    assert all(f.error is None for f in facts), [(f.path, f.error) for f in facts]
    _nodes, edges = graph_for(RepoIndex("r", facts), facts)

    def name(key: str) -> str:
        return key.rsplit(":", 1)[-1]

    return {(name(e["src"]), name(e["dst"]), e["kind"]): e["confidence"] for e in edges
            if e["kind"] in ("calls", "dispatches_to", "overrides")}


JAVA = {
    "core/Processor.java": """
        package bank.core;
        public interface Processor { void process(String code); String name(); }
    """,
    "core/AbstractProcessor.java": """
        package bank.core;
        import java.util.List;
        public abstract class AbstractProcessor implements Processor {
            private List<String> seen;
            public void process(String code) {
                if (code != null) { handleInAdvance(code); }
                this.handleLate(code);
                seen.add(code);
                code.trim();
            }
            protected abstract void handleInAdvance(String code);
            protected void handleLate(String code) { }
        }
    """,
    "loan/DueInAdvance.java": """
        package bank.loan;
        import bank.core.AbstractProcessor;
        public class DueInAdvance extends AbstractProcessor {
            protected void handleInAdvance(String code) { super.handleLate(code); }
            public String name() { return "due"; }
        }
    """,
    "loan/Fifo.java": """
        package bank.loan;
        import bank.core.AbstractProcessor;
        public class Fifo extends AbstractProcessor {
            protected void handleInAdvance(String code) { }
            protected void handleLate(String code) { }
            public String name() { return "fifo"; }
        }
    """,
    "loan/ProcessorFactory.java": """
        package bank.loan;
        import bank.core.Processor;
        public class ProcessorFactory {
            public Processor determineProcessor(String code) { return new Fifo(); }
        }
    """,
    "loan/OtherFactory.java": """
        package bank.loan;
        public class OtherFactory { public Object determineProcessor(String c) { return null; } }
    """,
    "svc/LoanService.java": """
        package bank.svc;
        import bank.core.Processor;
        import bank.loan.ProcessorFactory;
        public class LoanService {
            private final ProcessorFactory factory;
            @Autowired private ProcessorFactory injected;
            public LoanService(ProcessorFactory factory) { this.factory = factory; }
            void viaField(String c) { factory.determineProcessor(c); }
            void viaThisField(String c) { this.injected.determineProcessor(c); }
            void viaTypedLocal(String c) { final Processor p = factory.determineProcessor(c); p.process(c); }
            void viaVarLocal(String c) { var p = factory.determineProcessor(c); p.name(); }
            void viaChain(String c) { factory.determineProcessor(c).process(c); }
            void viaParam(Processor given, String c) { given.process(c); }
            void viaNew(String c) { new ProcessorFactory().determineProcessor(c); }
        }
    """,
}


def test_java_calls_follow_fields_parameters_locals_this_and_return_types():
    g = _graph(JAVA)
    det = "ProcessorFactory.determineProcessor"
    for caller in ("viaField", "viaThisField", "viaTypedLocal", "viaVarLocal", "viaChain", "viaNew"):
        assert g[(f"LoanService.{caller}", det, "calls")] == 1.0, caller
    # The like-named method of another factory is never confused with it.
    assert not any(dst == "OtherFactory.determineProcessor" for _s, dst, _k in g)
    # Through the interface: the declaration, then every implementation it may dispatch to.
    for caller in ("viaTypedLocal", "viaChain", "viaParam"):
        assert g[(f"LoanService.{caller}", "Processor.process", "calls")] == 1.0
        assert g[(f"LoanService.{caller}", "AbstractProcessor.process", "dispatches_to")] == DISPATCH_CONFIDENCE
    assert ("LoanService.viaVarLocal", "Processor.name", "calls") in g
    assert {s for s, d, k in g if d == "Fifo.name" and k == "dispatches_to"} == {"LoanService.viaVarLocal"}


def test_java_a_bare_call_on_this_reaches_the_abstract_declaration_and_each_override():
    g = _graph(JAVA)
    assert g[("AbstractProcessor.process", "AbstractProcessor.handleInAdvance", "calls")] == 1.0
    assert g[("AbstractProcessor.process", "DueInAdvance.handleInAdvance", "dispatches_to")] == DISPATCH_CONFIDENCE
    assert g[("AbstractProcessor.process", "Fifo.handleInAdvance", "dispatches_to")] == DISPATCH_CONFIDENCE
    assert g[("AbstractProcessor.process", "AbstractProcessor.handleLate", "calls")] == 1.0
    assert ("AbstractProcessor.process", "Fifo.handleLate", "dispatches_to") in g
    # super.m() is that one method: no dispatch.
    assert g[("DueInAdvance.handleInAdvance", "AbstractProcessor.handleLate", "calls")] == 1.0
    assert not any(s == "DueInAdvance.handleInAdvance" and k == "dispatches_to" for s, _d, k in g)
    # overrides run from a method to the supertype method it overrides.
    assert g[("DueInAdvance.handleInAdvance", "AbstractProcessor.handleInAdvance", "overrides")] == 1.0
    assert g[("AbstractProcessor.process", "Processor.process", "overrides")] == 1.0
    assert ("DueInAdvance.name", "Processor.name", "overrides") in g


def test_a_receiver_of_a_library_type_gets_no_edge_rather_than_a_guess_by_name():
    files = dict(JAVA)
    # A repository method named like List.add / String.trim must not catch library calls.
    files["util/Bag.java"] = "package bank.util; public class Bag { void add(String s) { } void trim() { } }"
    g = _graph(files)
    assert not any(s == "AbstractProcessor.process" and d in ("Bag.add", "Bag.trim") for s, d, _k in g)


def test_python_self_fields_typed_by_init_parameters_inherited_methods_and_return_annotations():
    g = _graph({
        "app/base.py": "class Base:\n    def save(self):\n        pass\n\n\n"
                       "class Repo(Base):\n    def get(self) -> 'Base':\n        return self\n\n\n"
                       "class SqlRepo(Repo):\n    def save(self):\n        pass\n",
        "app/svc.py": "from app.base import Repo\n\n\nclass Svc:\n"
                      "    def __init__(self, repo: Repo):\n        self.repo = repo\n\n"
                      "    def go(self):\n        self.repo.save()\n        self.repo.get().save()\n"
                      "        fresh = Repo()\n        fresh.save()\n",
    })
    assert g[("Svc.go", "Base.save", "calls")] == 1.0          # inherited, through the field's type
    assert g[("Svc.go", "Repo.get", "calls")] == 1.0
    assert g[("Svc.go", "SqlRepo.save", "dispatches_to")] == DISPATCH_CONFIDENCE
    assert g[("SqlRepo.save", "Base.save", "overrides")] == 1.0


def test_typescript_constructor_parameter_properties_and_csharp_and_go_receivers():
    g = _graph({
        "web/repo.ts": "export interface Store { save(): void }\n"
                       "export class Repo implements Store { save(): void {} }\n",
        "web/svc.ts": "import { Store } from './repo';\n"
                      "export class Svc { constructor(private readonly store: Store) {}\n"
                      "  go() { this.store.save(); } }\n",
        "net/A.cs": "namespace N; public interface IR { void Go(); } public class R : IR { public void Go() {} }\n"
                    "public class A { private readonly IR _r; public A(IR r) { _r = r; }\n"
                    "  public void M() { _r.Go(); this._r.Go(); } }\n",
        "g/s.go": "package g\ntype DB struct{}\nfunc (d *DB) Query() {}\ntype S struct { db *DB }\n"
                  "func NewS() *S { return &S{} }\nfunc (s *S) Run() { s.db.Query(); x := NewS(); x.Stop() }\n"
                  "func (s *S) Stop() {}\n",
    })
    assert g[("Svc.go", "Store.save", "calls")] == 1.0
    assert g[("Svc.go", "Repo.save", "dispatches_to")] == DISPATCH_CONFIDENCE
    assert g[("A.M", "IR.Go", "calls")] == 1.0 and ("A.M", "R.Go", "dispatches_to") in g
    assert g[("S.Run", "DB.Query", "calls")] == 1.0
    assert g[("S.Run", "S.Stop", "calls")] == 1.0              # x := NewS() is a *S


def test_parsers_record_declared_types_and_the_cache_carries_them():
    f = parse_file("svc/LoanService.java", JAVA["svc/LoanService.java"].encode())
    assert ("LoanService", "factory", ("ProcessorFactory",)) in {x[:3] for x in f.fields}
    assert ("LoanService", "injected", ("ProcessorFactory",)) in {x[:3] for x in f.fields}
    assert ("LoanService.viaVarLocal", "p", ("factory", "determineProcessor", CALL)) in {x[:3] for x in f.vars}
    assert ("LoanService.viaParam", "given", ("Processor",)) in {x[:3] for x in f.vars}
    assert ("LoanService", ("#t:ProcessorFactory", "determineProcessor"), True) in {
        (r.src.split(".")[0], r.chain, r.call) for r in f.refs}
    factory = parse_file("loan/ProcessorFactory.java", JAVA["loan/ProcessorFactory.java"].encode())
    assert factory.returns == {"ProcessorFactory.determineProcessor": ("Processor",)}
    again = row_to_facts(f.path, f.lang, f.module, f.content_hash, facts_to_row(f))
    assert (again.fields, again.vars, again.returns, again.version) == (f.fields, f.vars, f.returns, FACTS_VERSION)
    # An incremental index re-resolves a file when a type it names changes.
    assert "ProcessorFactory" in names_of(f) and CALL not in names_of(f)


@pytest.mark.parametrize("name, words", [
    ("DuePenFeeIntPriInAdvanceProcessor.handleTransactionThatIsPaymentInAdvanceOfInstallment",
     {"Due", "In", "Advance", "Processor", "handleTransactionThatIsPaymentInAdvanceOfInstallment", "Payment",
      "Installment"}),
    ("parse_http_request", {"parse_http_request", "parse", "http", "request"}),
    ("HTTPServer.getURL2", {"HTTPServer", "HTTP", "Server", "getURL2", "URL", "2"}),
])
def test_identifier_terms_split_code_names_into_words(name, words):
    assert words <= set(identifier_terms(name).split())
