# AI-Driven Development Workflow Orchestrator

A local application that takes a software feature from initial intent through to a
reviewed, merged pull request. It is an **orchestrator**: code comprehension,
planning, editing and review are delegated to existing agent harnesses (Claude
Code, Codex), while this app owns the state machine, process supervision, Git
integration and wiki automation that otherwise require manual coordination.

> **Status:** personal project, pre-1.0. The host runner and the full phase
> pipeline are exercised end to end; the Docker sandbox and PR creation against
> a real remote are documented below as unverified.

The design follows a Software Requirements Specification (SRS) that is not
part of this repository; the `FR-N`, `NFR-N`, `AC-N` and `§` references
throughout the code and this README point at its sections.

## Phases

A session progresses `qa → srs → plan → implement → review → merge`. No phase is
entered without a recorded, immutable approval of the preceding phase's artifact.
Sessions survive application restarts, and in-flight agent runs are reattached
rather than killed.

## Quick start

```bash
uv sync
uv run workflow-orchestrator db migrate
uv run workflow-orchestrator serve          # http://127.0.0.1:8000
```

To run agents in a sandbox (recommended — see NFR-1):

```bash
docker build -t workflow-agent:local -f docker/Dockerfile.agent .
export DOCKER_IMAGE_AGENT=workflow-agent:local
```

Without Docker the supervisor falls back to detached host subprocesses. That is
convenient for development but drops container isolation; set
`WORKFLOW_ALLOW_HOST_RUNNER=false` to require Docker.

> **Docker was not running on the development machine when this was built.** The
> Docker runner's launch contract is pinned by `tests/test_docker_runner.py`
> (detached, non-root, worktree-only mount, capabilities dropped, credential
> *names* forwarded), but the image has not been built and no container has been
> launched. The host runner is fully exercised end to end, including the
> restart-and-reattach and cost-ceiling acceptance criteria. Before relying on
> the sandbox, run the manual verification below.

```bash
docker build -t workflow-agent:local -f docker/Dockerfile.agent .
docker run --rm workflow-agent:local          # should print both CLI versions
```

## Configuration

All configuration is environment-driven; see `src/workflow_orchestrator/config.py`
for the full table. Credentials are read from the environment only and are never
written to the database.

| Variable | Required | Notes |
|---|---|---|
| `AWS_BEARER_TOKEN_BEDROCK` | one of | Bedrock API key — preferred if set |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | one of | SigV4 fallback |
| `AWS_PROFILE` | one of | Named-profile fallback |
| `AWS_REGION` | yes | e.g. `us-east-1` |
| `WORKFLOW_BEDROCK_MODEL_REASONING` | no | Default `us.anthropic.claude-sonnet-4-6` — see note below |
| `WORKFLOW_BEDROCK_MODEL_BUDGET` | no | Default `amazon.nova-lite-v1:0` |
| `DOCKER_IMAGE_AGENT` | recommended | Image with `claude` and `codex` on `PATH` |
| `WORKFLOW_DB_PATH` | no | Default `~/.workflow-orchestrator/db.sqlite3` |
| `WORKFLOW_LOG_DIR` | no | Default `~/.workflow-orchestrator/logs/` |
| `WORKFLOW_HOST` / `WORKFLOW_PORT` | no | Default `127.0.0.1:8000`, loopback enforced |
| `WORKFLOW_RUN_TIMEOUT_MINUTES` | no | Default `90` |
| `WORKFLOW_RUN_COST_CEILING_USD` | no | Default `5.00` |

### A note on the reasoning model

The SRS specifies Sonnet 5 for the requirements engine. That model is **not
currently enabled on this AWS account** — Bedrock's Converse API rejects it with:

```
AccessDeniedException: anthropic.claude-sonnet-5 is not available for this account
```

The default is therefore `us.anthropic.claude-sonnet-4-6`, the model the existing
Neovim provider already runs on, which is verified working. Once Sonnet 5 access
is granted, switch with no code change:

```bash
export WORKFLOW_BEDROCK_MODEL_REASONING=us.anthropic.claude-sonnet-5
```

## Wiki repository

The Librarian reads from and writes to a *consolidated wiki repo*, laid out per
SRS §4.5 with page directories directly under `llm-wiki/`. Scaffold one with:

```bash
uv run workflow-orchestrator wiki init /path/to/wiki --seed-from /path/to/existing/vault
```

An existing vault that nests its page directories under `llm-wiki/wiki/` is read
as-is — the layout is detected, not assumed (SRS §1.2 still forbids migrating
it). `wiki_repo_path` may point at the repo root, at `llm-wiki/`, or at the
nested page directory; all three resolve to the same repo.

**The QA phase is only as good as this context.** A super summary that does not
resolve costs nothing visible — the model simply asks generic questions. Check
it before starting a session:

```bash
curl -s localhost:8000/sessions/<id>/qa/context | python -m json.tool
```

`injected: false` with `available: true` means the loop opened without context;
**Restart with wiki context** in the QA panel discards the transcript and
re-opens it (context can only be injected at round 0).

### The as-built record

The SRS states what was *proposed*. Ingesting only that leaves the wiki
recording intentions as facts — a session that drops a requirement leaves no
trace, and the next one is told the feature exists.

So after a pull request merges, the write-back runs the configured **reviewing**
harness once more to produce `.workflow/as-built.md`: every `FR-N`/`NFR-N`/`AC-N`
marked `implemented`, `deviated`, `dropped` or `unverified`, each with the
`file:line` where it lives. That document is ingested *after* `srs.md`, framed
so the Librarian knows it supersedes the specification where the two disagree.

Post-merge is the only honest moment. Fixes land during PR review, maintainers
amend, and a squash rewrites the branch — a record written when the PR opened
describes a state that no longer exists. Ground truth is `gh pr diff`, not the
local branch.

`review.md` is deliberately **not** ingested. It is written before the fix runs
it triggers, so it asserts bugs that were already repaired.

The panel on a completed session lists dropped and deviated requirements first;
`GET /sessions/<id>/as-built` returns the same data. Set
`WORKFLOW_POST_AS_BUILT_COMMENT=true` to also mirror the record onto the merged
PR — off by default, because it publishes to GitHub.

## Tests

```bash
uv run pytest                # unit + integration + acceptance
uv run pytest -m acceptance  # SRS §7 criteria only
```

### SRS §7 acceptance criteria

| Criterion | Covered by | Status |
|---|---|---|
| AC-1 session persistence across restart | `test_acceptance_persistence.py` (real uvicorn + `SIGKILL`) | automated |
| AC-2 phase blocked without approval | `test_api_approvals.py` | automated |
| AC-3 independent harness selection | `test_api_projects_sessions.py` (+ migration checks) | automated |
| AC-4 run reattach after restart | `test_run_supervisor.py` | automated (host runner) |
| AC-5 cost ceiling termination | `test_run_supervisor.py` | automated (host runner) |
| AC-6 wiki write serialisation | `test_acceptance_wiki.py` | automated |
| AC-7 `index.md` token logging | `test_librarian.py` | automated |
| AC-8 super summary approval gate | `test_wiki_api.py` | automated |
| AC-9 harness adapter isolation | `test_harness_isolation.py` (AST check) + live run of both adapters | automated + verified live |
| AC-10 PR creation and merge detection | `test_merge.py` (PR title/body, merge → completed) | partially automated — `gh pr create` against a real GitHub remote is not exercised |

### What was verified against live services

- **Bedrock QA loop** → produced a complete 11.5 KB SRS with all nine required
  sections, `FR-N`/`NFR-N` identifiers and worked acceptance criteria.
- **Both harness adapters** → the same `srs.md` yielded a `plan.md` from Claude
  Code *and* Codex through one interface, with no file outside `.workflow/`
  modified (AC-9).
- **Detached implement run** → 3.5 minutes, 14,096 tokens, $1.22, producing
  working code that cited the `FR-N` identifiers from the generated SRS. Meters,
  the run log and the diff endpoint all behaved.

### Session scratch never reaches a commit (§4.5)

`.workflow/` holds the SRS, plan, review, diff and run logs. Those are wiki
input, not pull-request content, and two independent guards keep them out:

1. `.workflow/` and `worktrees/` are written to **`.git/info/exclude`**, not to
   `.gitignore`. `.gitignore` is tracked, so editing it would modify the target
   repository just because a session was created — and it would not even work:
   `git worktree add --detach <ref>` checks out a commit, and an uncommitted
   `.gitignore` edit is not in one. `info/exclude` resolves to the shared git
   dir, so one write covers every worktree and is never committed.
2. `commit_all` stages with `git add -A -- . ':(exclude).workflow'`. The
   pathspec holds even in a repository whose ignore rules are missing,
   overridden by a `!` rule, or checked out from an older commit.

Not verified live: the Docker sandbox (daemon unavailable — see above) and
`gh pr create` / merge polling against a real GitHub remote.
