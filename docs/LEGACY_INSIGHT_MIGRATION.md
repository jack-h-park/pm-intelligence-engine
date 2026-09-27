# Historical Insight migration API

The external legacy inventory is a private, hash-verified snapshot of Gate 0,
Engine, and wiki metadata. It does not change the original records. The
Candidate inventory and import routes are separate and reject an external
legacy inventory.

1. `POST /insight-migration-inventories/legacy` stores the full manifest as an
   inert batch. The response returns its ID, hash, record count, and unresolved
   count. An identical manifest hash returns the original batch ID.
2. `GET /insight-migration-inventories/legacy/{id}/records` pages at most 200
   records and can filter by `classification` or `migration_state`. Use its
   `next_offset` until it is null; check the same manifest hash on every page.
3. `POST /insight-migration-inventories/legacy/{id}/plans` records a complete
   disposition proposal. Every decision names the original system, type, ID,
   source hash and snapshot revision, and selects a disposition. Missing-source
   records must remain deferred. `review_reference` records where the human
   review can be found; a caller-supplied string is not proof of approval.
   A decision may explicitly set `suppress_legacy_reminder=true` only for a
   resolved Gate 0 sensing file with `preserve_reference`, and must name its
   `replacement_insight_id`. Both values are bound into the plan hash. Omitted
   or false suppression leaves existing plan hashes unchanged. Recording a
   plan creates no aliases or overlay, and the replacement ID is not yet
   verified at plan-record time.
4. `POST /insight-migration-inventories/legacy/{id}/imports` adds at most 100
   metadata aliases per call, keyed by inventory, origin, and revision. It
   requires the original manifest hash, the saved plan ID and hash, and a
   short-lived signed preflight receipt from the source-reading migration
   runner. A mismatch or expired receipt is rejected. The runner re-reads the
   sources before each batch. Each signed receipt includes a unique nonce and
   is consumed atomically with its import batch; it cannot authorize another
   batch. Repeat with a new preflight until `complete` is true. A retry adds no
   duplicate aliases.
5. `GET /insight-migration-inventories/legacy/{id}/aliases` pages the saved
   metadata for reconciliation. Compare its count and origin/revision fields
   against the reviewed plan before treating the batch as imported.
6. `GET /insight-migration-inventories/legacy/{id}/overlay` reports the
   external overlay's enabled state and revision. `GET .../overlay/suppressed`
   pages only the sensing origins explicitly selected by an active overlay;
   while disabled it returns an empty set. The listing includes the inventory
   hash and overlay revision so detector and Console readers can reject drift.
7. `POST /insight-migration-inventories/legacy/{id}/overlay` activates only
   when the import is complete, every alias matches the approved plan, the
   release flag and approved plan hash match, and a purpose-specific fresh
   source preflight is signed. Each selected replacement Insight must still be
   current and cite a source with the exact original URL. The request names
   `expected_revision`; each change appends an actor- and hash-bound audit
   event. Disabling needs no activation flag or Insight write gate and keeps
   the aliases and audit events. A concurrent or stale revision is rejected.

Import is disabled by default through `INSIGHT_LEGACY_IMPORT_ENABLED=false`
and an empty `INSIGHT_LEGACY_APPROVED_PLAN_HASH`. The operator must set both
the flag and the exact approved plan hash; another recorded proposal cannot
use the same import window. Configure a separate
`INSIGHT_LEGACY_PREFLIGHT_SECRET` of at least 32 characters for the Engine and
the controlled migration runner. An API bearer token alone cannot attest that
the source classifier was rerun.

Before enabling it, rerun the source classifier against authenticated Engine
reads and the current Gate 0 and wiki sources, reconcile changed and unresolved
records, and take a new consistent recovery snapshot. Preserve the runner's
preflight receipt. Its signature has a five-minute lifetime; the runner must
actually re-read and compare all source revisions before signing each batch.
The flag is an operational gate, not an E10 default-cutover authorization.

The import writes only `intelligence_migration_aliases`. It does not create
Insights, change source or decision records, send notifications, call a model,
spend an allowance, or enable the migration overlay. An imported suppression
decision remains inert until the separately gated overlay activation. The
Engine API alone does not establish reader agreement, fresh source coverage,
or release approval; the source-reading runner and every legacy reminder
consumer must use the same revision before production activation. The existing
per-origin overlay and Candidate importer remain independent. Rollback
disables readers and retains original records and alias receipts; it does not
restore an old database over newer decisions.
