-- 0009: the analyst workbench (Maltego-style workflow on top of the assertion log).
--
-- Annotations, saved views, machines and their runs are ANALYST WORKSPACE data. None of it is
-- evidence and none of it is an assertion: a note on an entity does not make a claim about the
-- world, and the projection never reads these tables. Everything an action produces that IS a
-- claim still goes through the assertion writer with evidence.

CREATE TABLE graph_annotation (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  case_id       uuid NOT NULL,
  cluster_id    uuid,                                    -- NULL = a note on the whole graph
  kind          text NOT NULL CHECK (kind IN ('note', 'pin', 'colour', 'label', 'group')),
  body          text NOT NULL CHECK (length(body) <= 4000),
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX graph_annotation_case ON graph_annotation (case_id, cluster_id);

CREATE TABLE graph_view (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  case_id       uuid NOT NULL,
  name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  layout        text NOT NULL,
  state         jsonb NOT NULL,                          -- node positions, filters, hidden kinds
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, name)
);

-- A machine is a reusable multi-step transform chain, like a lens but over graph entities.
CREATE TABLE machine (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  name          text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
  version       integer NOT NULL CHECK (version >= 1),
  definition    jsonb NOT NULL,
  sha256        text NOT NULL,
  origin        text NOT NULL CHECK (origin IN ('local', 'imported')),
  trust         jsonb NOT NULL DEFAULT '{}',
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, name, version)
);
CREATE TRIGGER machine_append_only BEFORE UPDATE OR DELETE ON machine
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();

-- A run is proposed first (what it would ask, how many calls at most) and runs only when approved.
CREATE TABLE machine_run (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  case_id       uuid NOT NULL,
  machine_name  text NOT NULL,
  machine_version integer NOT NULL,
  seeds         jsonb NOT NULL,
  status        text NOT NULL CHECK (status IN ('proposed', 'approved', 'running', 'done', 'failed')),
  allow_target_contact boolean NOT NULL DEFAULT false,
  trace         jsonb NOT NULL DEFAULT '[]',
  created_by    uuid NOT NULL,
  approved_by   uuid,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX machine_run_case ON machine_run (case_id, created_at DESC);

CREATE TABLE machine_schedule (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  case_id       uuid NOT NULL,
  machine_name  text NOT NULL,
  seeds         jsonb NOT NULL,
  interval_minutes integer NOT NULL CHECK (interval_minutes >= 5),
  enabled       boolean NOT NULL DEFAULT true,
  last_run_at   timestamptz,
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migration (version) VALUES ('0009_workbench');
