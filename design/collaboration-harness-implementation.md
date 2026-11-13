# Collaboration harness / 2026-11-06

Baseline: dev d364713. Reference reviewed: deepseek-ai/dsh-superagent-prototype
8c32a9dc5d72c033816c6a8110b37f40cf94fba4, docs/001-domain.md and docs/003-execution.md.
Adapt its stable task / individual assignment separation and persist-before-call
principle; do not install its unrestricted execution policy.

Stages: (1) durable store/scheduler, (2) Team adapter and state projection,
(3) fictional project evidence contract/real-provider acceptance,
(4) pnpm dev, regression, screenshots and dev bundle.

SQLite BEGIN IMMEDIATE is authoritative. Handoff and execution intent are atomic;
external side effects are not exactly-once. Unknown calls need reconciliation,
never silent retry. One active attempt/unit and one slot/member; resource conflicts
are independent of DAG edges. Failure does not block unrelated work. Runtime
has only a store and executor port; TeamManager is restricted to an adapter.
Nonempty text is structural validation, not proof of correctness. Human acceptance
remains pending. No fake Leader output and no silent reassignment.

Attached materials describe but do not include original project files. Newly
written fictional fixtures must be labelled as such. Desired Kimi/Hermes roles
are not proof these executors are installed here. Existing ~/.crew data is untouched.
