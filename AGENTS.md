# Competitions Agent Context

This repository is a SIBADI student sports competitions management app.

## Purpose

Manage student sports competition participation records, imports, exports,
custom fields, and table views.

## Current Shape

- Backend: Sanic.
- Templates: Jinja2.
- Frontend: vanilla JavaScript.
- Runtime database: SQLite at `./data/competitions.sqlite3`.
- Docker container published on `127.0.0.1:8081` and intended to be proxied by
  nginx.
- Deployment templates target `dokin-app.online`.

## Key Files

- `README.md`
- `src/`
- `data/`
- `deploy/nginx/competitions.conf`
- `deploy/systemd/`
- `scripts/backup_sqlite.py`
- `scripts/migrate_mongo_to_sqlite.py`

## Notes

- `data/` is live application data and is mounted by the running container. Do
  not casually move, delete, or rewrite it.
- `.env` is local; use `.env.example` for tracked config shape.
- The `admin` role manages custom fields and destructive actions.
- The `editor` role is intended for data entry, imports, template download, and
  record edits.

## Safe Start

- Read `README.md`.
- For UI/data behavior, inspect `src/` before changing deploy files.
- For deployment changes, inspect `deploy/` and `docs/quickstart.md`; production operations follow the Production Operations section and require an explicit owner request.

## Current Product Priority

Competitions is currently a practical application for its existing real-world workflow.

Current priorities are:
- stability and data safety;
- completing real user workflows;
- fixing bugs discovered during actual use;
- improving usability where it saves real user time;
- completing calendar/import workflows;
- improving student/account administration where required.

Do not turn the project into a generic platform, SaaS product, multi-organization system or universal competition-management product unless explicitly requested.

Do not add abstractions or configurability solely for hypothetical future users.

SQLite is an intentional architecture choice for the current scale. Do not replace it merely for hypothetical future growth.

## Available Project Agents

The workspace provides these specialized subagents:

- `competitions-lead` — requirements, architecture, data risks, implementation planning and acceptance criteria.
- `competitions-ui` — UI/UX analysis for tasks that materially affect user-facing workflows.
- `competitions-developer` — implementation of features and bug fixes.
- `competitions-qa` — independent verification, regression testing, data-integrity checks and acceptance testing.
- `competitions-deploy` — local runtime/startup and smoke verification; production operations (deployment, rollback, DB backup/restore, troubleshooting) on the owner's explicit request within the Production Operations rules.

Use specialized agents according to their roles instead of having the main session duplicate their work.

## Default Autonomous Development Workflow

For implementation tasks, the main session acts as the orchestrator.

Unless the user explicitly requests another workflow, use:

Lead
→ UI when needed
→ Developer
→ QA
→ Deploy verification

After QA PASS there are two completion modes:

Normal development:
→ local Deploy verification
→ completion

When the owner's request explicitly includes production deployment:
→ local verification when useful
→ production Deploy (see Production Operations)
→ production health/smoke verification
→ completion

Do not deploy every implementation task automatically merely because production access exists. Production deployment happens only when the owner explicitly requests it or the original task explicitly includes deployment.

The main session coordinates the workflow and carries relevant outputs between agents.

### 1. Lead Analysis

Invoke `competitions-lead`.

Provide the original user requirement and relevant context.

Obtain:
- current behavior;
- requested behavior;
- affected components and data;
- documentation impact;
- implementation plan;
- database/migration considerations;
- security and compatibility considerations;
- risks;
- acceptance criteria;
- QA verification points.

Do not begin implementation when a material product ambiguity remains unresolved.

### 2. UI Analysis When Needed

Invoke `competitions-ui` only when the task materially changes UI or UX.

Examples:
- forms;
- navigation;
- tables;
- data-entry workflows;
- calendar interaction;
- import/export interaction;
- user/account administration;
- moderation workflows;
- validation/error presentation;
- significant visual changes.

Do not invoke UI for backend-only, migration-only, test-only or trivial visual changes.

Pass relevant UI guidance and UI acceptance criteria to Developer and QA.

### 3. Implementation

Invoke `competitions-developer`.

Provide:
- original requirement;
- Lead plan;
- acceptance criteria;
- relevant UI guidance;
- known data, security and compatibility risks;
- documentation update requirements identified by Lead (see Documentation Hygiene).

Developer owns implementation.

The main/orchestrator should not independently duplicate Developer's implementation work.

### 4. Independent QA

After implementation, invoke `competitions-qa`.

Provide:
- original requirement;
- acceptance criteria;
- Developer summary;
- Lead QA verification points;
- UI acceptance criteria when applicable.

QA must independently verify the result.

QA must explicitly determine whether the change has documentation impact instead of always requiring documentation updates. Do not block a small change merely because its documentation does not need updating.

QA should pay particular attention when applicable to:
- existing SQLite databases and migrations;
- data integrity;
- authentication;
- roles and permissions;
- Excel import/export;
- duplicate handling;
- calendar/competition relationships;
- attachments;
- destructive operations;
- user-visible regressions;
- documentation impact of the change (see Documentation Hygiene);
- repository hygiene: no accidentally added sensitive, local or generated files;
- that new tests, fixtures and examples follow the synthetic-data-only rule (see Data Safety).

If QA returns PASS, continue to local runtime verification.

If QA returns FAIL, return the defect report to Developer and then invoke QA again after the fix.

## Repair Cycle Limit

Allow at most 2 repair cycles for the entire task.

A repair cycle begins only when QA or Deploy returns FAIL and the task is returned to Developer for correction.

Analysis, clarification, interrupted sessions or recovery from an interrupted agent run do not count as repair cycles.

After 2 failed repair cycles, stop the autonomous workflow and report:
- what remains broken;
- relevant QA/deployment errors;
- what was attempted;
- likely blocker.

Do not continue consuming repair cycles indefinitely.

### 5. Local Runtime Verification

After QA PASS, invoke `competitions-deploy`.

This stage is LOCAL verification only.

Verify:
- supported local runtime can be prepared;
- application starts successfully;
- expected local listener is available;
- HTTP/health smoke check succeeds;
- startup/database initialization does not produce a blocking error.

Do not deploy to the real VPS automatically; production deployment is governed by the Production Operations section.

If Deploy returns FAIL because of an application/repository problem:
- count it as a repair cycle;
- return the failure to Developer;
- run QA again after the fix;
- retry Deploy only after QA PASS.

If failure is purely an external/local environment problem, stop and report it rather than changing application code unnecessarily.

## User Clarification and Stop Conditions

The autonomous workflow should minimize unnecessary interruptions, but it must not guess when an unresolved question can materially change the result.

Stop and ask the user before continuing when:
- the requirement has multiple materially different interpretations;
- expected product behavior is meaningfully ambiguous;
- an important requirement or acceptance criterion is missing;
- a decision significantly affects stored data, database migration, UX, architecture, compatibility or security;
- a change could cause data loss or destructive behavior;
- an action crosses an existing safety boundary;
- existing project behavior conflicts with the requested behavior;
- an agent discovers a blocker requiring a product decision rather than an engineering decision;
- proceeding requires a significant assumption about what the user wants.

When asking the user:

1. Pause the autonomous workflow.
2. Preserve results already produced.
3. Briefly explain the ambiguity or blocker.
4. Present relevant options when known.
5. Explain practical differences when useful.
6. Ask one focused question.
7. Wait for the user's answer.

After the answer, resume from the appropriate stage instead of restarting the entire workflow unnecessarily.

Do not interrupt the user for routine engineering decisions that can reasonably be derived from:
- existing code;
- AGENTS.md;
- repository documentation;
- established project patterns;
- Lead analysis;
- normal engineering judgment.

If different reasonable answers would produce meaningfully different user-visible behavior, stored data, migration behavior, permissions, security properties or task scope, ask.

Otherwise make the smallest reasonable engineering decision and continue.

## Data Safety

Treat `data/` and existing SQLite databases as important persistent application data.

Never perform destructive testing against the real application database.

For development and QA:
- prefer temporary or synthetic databases where appropriate;
- preserve compatibility with existing databases;
- prefer safe additive migrations;
- test migration behavior when schema changes are involved;
- never silently discard existing records.

Never use real student identities, passwords, production exports, private attachments or other sensitive data in tests/examples.

Use synthetic data only, for:
- tests;
- fixtures;
- documentation;
- examples;
- screenshots;
- sample configuration;
- demo files;
- import/export examples.

Synthetic data must be fully fictional and safe for public publication.

Do not use real data with a few characters replaced, partially masked production records, or slightly modified copies of real data.

To reproduce a production bug, create a minimal synthetic fixture that reproduces the failing structure or condition without copying real data.

## Production Operations

The owner may explicitly request production operations. The goal is not to prevent competitions-deploy from operating production: after a high-level request, routine deploy/rollback/backup/restore/troubleshooting work is performed autonomously and verified, production data is kept recoverable, a known previous state is preserved before risky changes, and every operation is verified. Additional confirmation is required only when crossing into a materially different destructive or infrastructure operation. Competitions is a small single-instance application on one VPS — prefer simple, documented, repeatable procedures (`deploy/`, `scripts/`, `docs/quickstart.md`), not an enterprise deployment system.

Local development, local testing and safe local application startup remain allowed without any request.

### Production Access

competitions-deploy may SSH to the production VPS and perform production operations within this section.

Never add SSH private keys, credentials, server secrets or local credential file contents to the repository. If SSH credentials are already available to ZCode through the local environment, use that existing mechanism. Do not copy credentials into the repository and do not print private keys or secrets into reports or logs.

### Explicit Production Commands

An explicit owner request for a production operation authorizes the whole normal technical sequence required for that operation, for example:

- "задеплой на прод" / "обнови прод";
- "откати последний деплой" / "откати прод на <revision>";
- "сделай backup production DB";
- "восстанови production DB из <backup>";
- "проверь production";
- "перезапусти Competitions на production".

After such a request, do not make the owner run SSH commands manually or confirm each routine step separately. The authorization covers only the requested operation and the routine actions it requires.

### Normal Production Deployment

After an explicit deployment request, competitions-deploy may autonomously:

- SSH to the production VPS;
- inspect the current deployment state;
- inspect the current Git revision;
- fetch/pull the intended revision using the established deployment procedure;
- build/update Docker Compose services;
- restart/recreate application containers when required;
- perform required safe application/database initialization;
- inspect application/deployment logs;
- perform health checks and smoke checks;
- verify the deployed revision;
- roll back automatically when the deployment it just performed fails verification and rollback can be done safely.

Before deployment:

- QA must be PASS for the revision being deployed;
- repository hygiene must be PASS;
- the exact revision/commit being deployed must be identifiable;
- pre-existing unrelated production problems must not be silently treated as deployment success.

If the deployment includes a schema migration or otherwise has meaningful data risk, create a production DB backup before applying the change.

After deployment report:

- deployed revision;
- previous revision;
- backup created, if applicable;
- actions performed;
- health/smoke result;
- final production state;
- rollback result if rollback was necessary.

### Rollback

When explicitly requested, competitions-deploy may roll production back to a previous known revision.

Before rollback:

- identify the current revision;
- identify the target revision;
- determine whether database/schema compatibility creates a data risk;
- preserve current data before any destructive or potentially incompatible operation.

After rollback:

- verify application startup;
- perform healthcheck/smoke check;
- report the final running revision.

Do not assume that application rollback automatically implies database rollback. Database rollback/restore is a separate operation unless it is explicitly required and authorized.

### Production Database Backup

competitions-deploy may create production SQLite backups when:

- explicitly requested by the owner;
- required by the established deployment procedure;
- required as a safety step before an authorized migration, rollback or restore.

Use the project's established SQLite-safe backup mechanism (`scripts/backup_sqlite.py` / `src/backup.py`: sqlite3 backup API, gzip, attachments zip, retention). Verify backup integrity with the existing project mechanism (`PRAGMA quick_check`). Do not copy production backups into the Git repository. Do not expose their contents in agent output.

### Production Database Restore

Production DB restore is allowed when explicitly requested by the owner. A restore request authorizes competitions-deploy to perform the normal technical steps necessary for the restore.

Before replacing/restoring the production database:

1. identify the currently active production DB;
2. identify the requested backup unambiguously;
3. prevent application writes while the database is being replaced/restored (stop the application via the established procedure);
4. create a safety backup of the CURRENT production database before replacing it;
5. verify that the source backup exists and is usable.

Then:

6. restore using the established project procedure;
7. verify SQLite integrity (`PRAGMA quick_check` or the existing equivalent);
8. start/resume the application;
9. perform healthcheck/smoke verification.

Never overwrite the current production DB during restore without first preserving it, unless the owner explicitly orders otherwise and acknowledges that consequence.

Report:

- source backup restored;
- safety backup created from the previous DB;
- integrity-check result;
- application health result;
- final state.

### Production Troubleshooting

If the owner asks to inspect/check/troubleshoot production, competitions-deploy may:

- SSH to the VPS;
- inspect application/container status;
- inspect relevant logs;
- inspect disk/resource state relevant to Competitions;
- inspect Docker Compose state;
- inspect application health;
- restart the Competitions application through the normal procedure when this is a reasonable non-destructive recovery action.

Read-only diagnosis does not require separate confirmation for every command after the owner requested production troubleshooting.

Do not modify production records merely as a troubleshooting shortcut.

### Actions That Still Require Separate Explicit Authorization

Do NOT infer permission for the following merely from "deploy", "check production" or "fix production":

- manually editing production records;
- destructive SQL or manual data deletion;
- deleting production DB/backups/attachments;
- deleting Docker volumes containing persistent data;
- changing nginx configuration outside an explicitly requested infrastructure task;
- changing systemd configuration outside an explicitly requested infrastructure task;
- changing firewall/network/SSH configuration;
- changing production credentials/secrets;
- OS/package upgrades unrelated to the requested operation;
- rewriting Git history;
- force-push;
- destructive infrastructure changes unrelated to the requested operation.

If one of these becomes necessary, stop and explain why before performing it. If the owner explicitly requests that specific operation, it may be performed after the agent explains the impact and verifies the target.

## Documentation Hygiene

Documentation is part of the project and must stay accurate.

For significant changes, check whether the following need updating:
- README;
- architecture documentation;
- run and deployment instructions;
- configuration examples;
- user workflow descriptions;
- documentation on data, migrations, backup/restore and security, when the change affects them;
- any other documentation the change makes stale.

Documentation review is part of QA for significant changes, not a separate task the developer has to remember manually.

Do not update documentation formally when the change does not actually affect it.

## Repository Hygiene

The repository can become public, and the project works with student data. Before completing a significant task, check repository hygiene.

The public Git repository of Competitions must not contain:
- real personal data;
- real names of students or users;
- real accounts and logins belonging to real people;
- real passwords;
- password exports;
- production credentials;
- API keys, tokens, cookies and session data;
- contents of `.env`;
- private certificates and keys;
- the production SQLite database;
- real database dumps and backups;
- Excel imports/exports containing real student data;
- private attachments;
- logs with personal, sensitive or internal data;
- accidental temporary files;
- debug output;
- screenshots with real data;
- generated artifacts that should not live in the repository;
- local AI/IDE tool files, unless they are a deliberate part of the project.

Treat the production DB, production Excel files, password exports and private attachments as sensitive even when they are temporarily used for debugging. Do not copy them into the repository to reproduce a problem.

## Git Commit and Push

Normal development must be able to reach a deployable revision without the owner typing Git commands manually.

After all of the following hold:

- Developer completed the implementation;
- QA is PASS;
- repository hygiene is PASS;
- relevant documentation is updated;

ZCode may commit the completed changes and push them:

- commit on a `feature/*` branch created for the task (do not commit directly to `main`);
- push that `feature/*` branch to origin.

Direct pushes to `main`/`dev`, merges into them, tags and releases still require an explicit owner request. Reason: the repository's documented workflow (README, «Участие в разработке») keeps merging into `main`/`dev` as an owner review decision, so enabling direct push to `main` would conflict with the existing setup. The feature-branch rule is the smallest safe alternative: a pushed `feature/*` revision is fully deployable, so the owner can deploy it by revision or ask for a merge.

Still not allowed without separate explicit authorization:

- force-push;
- automatic merges of unrelated branches;
- rewriting Git history;
- creating tags or releases.

## Pre-Commit / Pre-Push / Release Review

Before commit, push or release, when it is part of the permitted workflow, check:
1. `git status`;
2. new and changed files;
3. `git diff`;
4. absence of secrets and sensitive data;
5. absence of accidental generated/local/debug files;
6. that `.gitignore` is up to date;
7. that documentation is up to date relative to the change.

This does not cancel the remaining restrictions: Git history rewriting, force-push, merges into `main`/`dev` and tags/releases without an explicit owner request, and the production actions that require separate authorization under Production Operations.

If potentially sensitive data is found, do not assume it is publishable. Stop and ask the project owner for a decision.

If sensitive data is already present in Git history, removing it from the current working tree is not enough. Report it to the project owner separately. Do not automatically rewrite Git history, force-push or rotate credentials without explicit permission.

## Completion Criteria

Do not report an implementation task complete merely because code was written.

Normal completion requires:

Developer implementation
→ QA PASS
→ local Deploy PASS

When the owner's request explicitly includes production deployment, completion additionally requires production Deploy PASS: the deployed revision verified, production health/smoke checks green, and the post-deployment report items from Production Operations.

When complete, report:
- what changed;
- whether UI agent was involved;
- database/migration impact;
- documentation impact and documentation updates performed, if any;
- repository hygiene check result;
- QA result;
- local runtime/smoke result;
- important limitations or follow-up items.

## Main Session Responsibilities

The main session is the orchestrator and user-facing coordinator.

For implementation tasks:
- delegate analysis to Lead;
- delegate UI/UX work to UI when relevant;
- delegate implementation to Developer;
- delegate verification to QA;
- delegate local runtime verification to Deploy;
- delegate production operations to Deploy when explicitly requested by the owner;
- commit and push completed work according to the Git Commit and Push rules;
- carry relevant results between agents;
- enforce the repair-cycle limit;
- stop for user clarification when required.

Do not invoke every agent mechanically when its role is irrelevant.

For questions, explanations, repository inspection, planning or other tasks that do not require implementation, answer directly or use only the relevant agent.

Do not run the full development pipeline unnecessarily.

## Recovery After Interrupted Runs

If an autonomous workflow is interrupted:

1. Inspect the current conversation context.
2. Inspect actual working-tree state.
3. Determine which agents actually produced complete usable results.
4. Do not assume an agent completed merely from a stale status label.
5. Do not repeat completed stages unnecessarily.
6. Resume from the earliest incomplete stage.
7. Preserve valid implementation already present in the working tree.

If completion state cannot be determined reliably and guessing could materially affect the task, ask the user.

An interrupted run by itself is not a repair cycle.

## Engineering Principles

Before changing code, inspect the existing implementation.

Prefer:
- minimal changes;
- existing project patterns;
- readable code;
- explicit error handling;
- appropriate regression tests;
- safe database evolution;
- maintainability.

Avoid:
- speculative features;
- unrelated refactoring;
- unnecessary dependencies;
- unnecessary frameworks;
- architecture for hypothetical future scale.

The goal is a useful, stable and maintainable Competitions application for its actual workflow.
