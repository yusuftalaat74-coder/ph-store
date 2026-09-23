-- ============================================================================
-- 0002. TRUNCATE GUARD ON THE FOUR APPEND-ONLY TABLES
-- ============================================================================
--
-- `trg_*_append_only` in 0001 is `BEFORE UPDATE OR DELETE ... FOR EACH ROW`.
-- A row-level trigger never fires for TRUNCATE — Postgres does not produce
-- per-row events for it — so `TRUNCATE state_transition` emptied the table
-- without raising, which was found by an independent re-verification on
-- 23 Sep 2026. The append-only guarantee had a hole exactly the width of one
-- keyword.
--
-- Closed here with a statement-level `BEFORE TRUNCATE` trigger on each of the
-- four tables, reusing the same `rova_forbid_mutation()` function so the
-- refusal message is identical whichever route is attempted.
--
-- Forward-only (A2.7): there is no downgrade. Removing this would be
-- re-opening the hole, which is not a migration, it is a decision.

CREATE TRIGGER trg_ledger_entry_no_truncate BEFORE TRUNCATE ON ledger_entry
  FOR EACH STATEMENT EXECUTE FUNCTION rova_forbid_mutation();

CREATE TRIGGER trg_audit_event_no_truncate BEFORE TRUNCATE ON audit_event
  FOR EACH STATEMENT EXECUTE FUNCTION rova_forbid_mutation();

CREATE TRIGGER trg_state_transition_no_truncate BEFORE TRUNCATE ON state_transition
  FOR EACH STATEMENT EXECUTE FUNCTION rova_forbid_mutation();

CREATE TRIGGER trg_traceability_event_no_truncate BEFORE TRUNCATE ON traceability_event
  FOR EACH STATEMENT EXECUTE FUNCTION rova_forbid_mutation();
