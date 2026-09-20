# Acceptance of the delivered configuration

Before dispatch, record these required facts in the packet or a caller-owned
`acceptance-plan.json`. The controller defines the intended delivery and assigns
gates; the author reports its checks, the verifier exercises the fixed delivery,
and the controller accounts for integrated gates before accepting the product.

| Required slot | Record |
|---|---|
| Delivery | Executable/package/service/data/answer, observable contract, source scope and intended use |
| Configurations | Applicable build modes, feature combinations, platforms and supported runtimes; identify the configuration actually delivered |
| Gates | Per configuration: owner, execution-plan path and command positions/argv, finite timeout, oracle/fixture identity, tool versions and effective settings |
| Artifacts | Intended paths and behavior checks; after execution, actual paths/hashes bound to the source snapshot and attempt |
| Coverage | Per gate: passed, failed, timeout, not_run, not_applicable or excluded; evidence or reason, owner and remaining action/closure condition |

These are **caller facts, not new protocol or executor fields**. A packet still
uses objective/scope/acceptance/inputs/known_facts/constraints. A `delivery.py` plan
still contains only commands/artifacts/dependencies/environment. Keep their roles
separate and pin the caller plan alongside the verifier's acceptance evidence.
Command positions are one-based within that pinned plan version. Changing order,
commands or scope requires updating the mapping; the helper does not reconcile it.

Select configurations from the requested deliverable and project requirements.
For a compiled CLI normally delivered as an optimized binary, include development
and release checks plus a behavior check of the exact release artifact. A build
succeeding is not that behavior check. Debug assertions, optimization, conditional
compilation, feature flags and packaging can change behavior. For a script/data
task with no build modes, record that dimension as not_applicable with a reason;
check the relevant interpreter, package or data contract instead. Do not invent a
debug/release distinction for every language or test invalid feature combinations.

Record actual effective settings, including project config and ambient overrides;
`--release` alone does not prove default optimization/assertion/target settings.
Required platforms or runtimes unavailable here are not_run, with an owner and
follow-up. Dependencies missing, timed-out commands and tests never reached after
an earlier failure are not passing checks. A skip/exclusion needs a reason and
owner; it does not remove a required gate. The controller must resolve any change
to the delivery scope and version the plan before a narrower acceptance is valid.

Report coverage per configuration, not just a total test count. A timeout/failure
rejects the required gate; an unrun required gate leaves acceptance incomplete.
Return a concrete defect or blocking requirement. Rework repeats the failing
configuration and the declared matrix on a new snapshot/attempt using the pinned
oracle. Keep the old failure. A changed oracle or scope gets a new version and an
explanation of which earlier results no longer compare.

For a delivered source script, bind its path/hash in the snapshot manifest; an
empty executor `artifacts` list is valid when there is no built artifact. If the
delivery is a copied or packaged script, declare that output beneath `build/`,
check its contents/behavior and retain its hash. Do not invent a build just to
populate that list.

## One executable Rust example

[rust-cli-plan.json](../assets/verification/rust-cli-plan.json) is an executable
four-field plan for a dependency-free, native Unix Rust CLI named `item-count`.
Its contract is stdout `0\n` for no arguments, `3\n` for `alpha beta gamma`, exit 0
and empty stderr. Replace its binary name and oracle with the actual contract.
It records tool versions, runs development and release tests, builds release, and
checks the **built binary** with an independent output oracle. Artifact digests
come from the verification result, not invented values in the example.

Example caller coverage: native host only, default features only (this example has
no feature flags), development tests, release tests/build, release artifact smoke;
verifier owns these gates and controller owns final integration. Additional
platform/MSRV/package gates are not claimed by this example: use actual project
requirements to add them or record why they do not apply. Project fmt/lint gates
remain required where specified; they are not implied by `cargo test`.

Capture Cargo.toml, Cargo.lock, source, tests, fixtures and relevant configuration.
The plan uses locked offline builds, a fresh Cargo home and an attempt-local target
directory. It explicitly selects ordinary debug/release assertion settings and no
extra Rust flags for this example; adapt those values to the **actual delivery**.
Preflight toolchain and dependencies. Projects needing registry/git dependencies
must supply an authorized, pinned offline source or declare the dependency blocked;
do not silently use an old artifact or fetch missing dependencies to make this pass.

Copy/adapt the plan into the run, then use the [delivery procedure](delivery.md).
The helper stops on the first failed command: mark later commands not_run, or use
a separate fresh attempt to gather additional diagnostics. `passed=true` checks
only the supplied commands/evidence; even the completion gate cannot infer omitted
configurations. The controller must account for the caller coverage record before
recording accepted. Host settling remains a separate condition.
