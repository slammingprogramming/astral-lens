-- 0003: the coverage ledger. One row per plugin invocation made by an
-- investigation run: what was asked, of which source, and what came back —
-- including "unknown". It is what lets the UI say "searched 5 sources, 2 could
-- not be consulted" instead of an empty-looking result list (invariant 3).
--
-- Not layer 1: it asserts nothing about the world, only about what we did. It
-- holds the query text (which can be a person's name), so it is case-scoped and
-- removed by the case erasure path.

CREATE TABLE search_run (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id    uuid NOT NULL,
  case_id         uuid NOT NULL,
  run_id          uuid NOT NULL,                 -- groups the plugins of one run
  ran_at          timestamptz NOT NULL,
  principal_id    uuid NOT NULL,
  plugin_id       text NOT NULL,
  plugin_version  text,
  query           jsonb NOT NULL,
  status          text NOT NULL,                 -- ok | partial | error | unknown | skipped
  coverage        jsonb NOT NULL DEFAULT '[]',   -- [{source, outcome, note}]
  gaps            jsonb NOT NULL DEFAULT '[]',
  assertion_count integer NOT NULL DEFAULT 0,
  stats           jsonb NOT NULL DEFAULT '{}',
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX search_run_case ON search_run (case_id, run_id, id);

CREATE TRIGGER search_run_append_only BEFORE UPDATE OR DELETE ON search_run
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();

INSERT INTO schema_migration (version) VALUES ('0003_search_runs');
