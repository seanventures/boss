# fault-kit

Known-good sample apps the fault kit (`scripts/install/qa_fault.py`, `boss qa fault`) is tested on,
one per stack it supports, beside the plain Node app in `../plain-node/plain`:

* `python/` — a stdlib HTTP service (`python3 app.py`, `$PORT`) with `unittest` tests;
* `go/` — a `net/http` service (`go run .`, `$PORT`) with `go test` tests.

Each declares its run command in `boss.preview.command`, as a build BOSS made would. The tests
(engine/tests/test_qa_fault_kit.py, engine/tests/test_recovery_faults_are_told_apart.py) apply
every fault to a copy, prove the app's own check now fails the way the test plan describes, and
that BOSS names the failure.
