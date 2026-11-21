-- 0007: monitoring. A monitor is a saved name search that can be re-run; each run records what
-- the sources reported and what could not be asked, so the next run can show a diff. Not layer 1:
-- these rows assert nothing about the world, only about what was searched and seen.
-- watch_term is a list of names or identifiers an analyst wants flagged wherever they appear in
-- the case (separate from the face watchlist, which holds enrolled photographs).

CREATE TABLE monitor (
  id               uuid PRIMARY KEY,
  workspace_id     uuid NOT NULL,
  case_id          uuid NOT NULL,
  name             text NOT NULL,
  query            jsonb NOT NULL,                 -- {name, country, schema, only}
  interval_minutes integer CHECK (interval_minutes IS NULL OR interval_minutes >= 5),   -- NULL = manual only
  active           boolean NOT NULL DEFAULT true,
  created_by       uuid NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX monitor_case ON monitor (case_id);

CREATE TABLE monitor_run (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  monitor_id    uuid NOT NULL REFERENCES monitor(id),
  run_id        uuid NOT NULL,                     -- the search_run group this run produced
  ran_at        timestamptz NOT NULL,
  facts         jsonb NOT NULL,                    -- {plugin: {fact key: label}} reported by THIS run
  coverage      jsonb NOT NULL,                    -- {plugin: answered | not_asked}
  diff          jsonb NOT NULL,                    -- against the previous run of this monitor
  watch         jsonb NOT NULL DEFAULT '[]',       -- watch-term match keys seen at this run
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX monitor_run_monitor ON monitor_run (monitor_id, id);
CREATE TRIGGER monitor_run_append_only BEFORE UPDATE OR DELETE ON monitor_run
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();

CREATE TABLE watch_term (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('name', 'identifier')),
  value         text NOT NULL CHECK (length(value) BETWEEN 2 AND 300),
  note          text NOT NULL DEFAULT '',
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE UNIQUE INDEX watch_term_unique ON watch_term (case_id, kind, lower(value));

INSERT INTO schema_migration (version) VALUES ('0007_monitoring');
