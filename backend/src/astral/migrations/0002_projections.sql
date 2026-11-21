-- 0002 projections: layers 2 and 3. EVERYTHING here is derived and disposable.
-- The projector may TRUNCATE any of these tables at any time and rebuild them
-- from layers 0 and 1. Nothing in this file is a system of record.

CREATE TABLE proj_state (
  case_id          uuid PRIMARY KEY,
  rebuilt_at       timestamptz NOT NULL,
  assertion_high   bigint NOT NULL,       -- highest assertion id included
  judgment_high    bigint NOT NULL,
  projector_version text NOT NULL
);

-- entity_id -> cluster_id for every entity mentioned in the case
CREATE TABLE proj_entity_member (
  case_id     uuid NOT NULL,
  entity_id   uuid NOT NULL,
  cluster_id  uuid NOT NULL,
  PRIMARY KEY (case_id, entity_id)
);
CREATE INDEX proj_entity_member_cluster ON proj_entity_member (case_id, cluster_id);

-- canonical ids that stopped being canonical after a merge -> current id
CREATE TABLE proj_redirect (
  case_id     uuid NOT NULL,
  old_id      uuid NOT NULL,
  current_id  uuid NOT NULL,
  PRIMARY KEY (case_id, old_id)
);

CREATE TABLE proj_entity (
  case_id     uuid NOT NULL,
  cluster_id  uuid NOT NULL,
  schema_name text,
  caption     text,
  member_count integer NOT NULL,
  PRIMARY KEY (case_id, cluster_id)
);

-- One row per unretracted literal assertion, with subject mapped to its cluster.
-- Grades are carried as-is; independent_sources collapses shared origins.
CREATE TABLE proj_property (
  case_id        uuid NOT NULL,
  cluster_id     uuid NOT NULL,
  predicate      text NOT NULL,
  value          jsonb NOT NULL,
  assertion_id   bigint NOT NULL,
  source_grade   char(1) NOT NULL,
  info_grade     smallint NOT NULL,
  independent_sources integer NOT NULL,
  valid_from     timestamptz,
  valid_to       timestamptz,
  PRIMARY KEY (case_id, assertion_id)
);
CREATE INDEX proj_property_lookup ON proj_property (case_id, cluster_id, predicate);

-- Entity-to-entity edges. Edges carry their own assertion so provenance
-- travels with the line on the graph.
CREATE TABLE proj_edge (
  case_id          uuid NOT NULL,
  subject_cluster  uuid NOT NULL,
  predicate        text NOT NULL,
  object_cluster   uuid NOT NULL,
  assertion_id     bigint NOT NULL,
  source_grade     char(1) NOT NULL,
  info_grade       smallint NOT NULL,
  independent_sources integer NOT NULL,
  PRIMARY KEY (case_id, assertion_id)
);
CREATE INDEX proj_edge_subject ON proj_edge (case_id, subject_cluster, predicate);
CREATE INDEX proj_edge_object  ON proj_edge (case_id, object_cluster, predicate);

-- Merges the not_same constraint refused, kept visible rather than dropped.
CREATE TABLE proj_conflict (
  case_id      uuid NOT NULL,
  judgment_id  bigint NOT NULL,
  detail       text NOT NULL,
  PRIMARY KEY (case_id, judgment_id)
);

INSERT INTO schema_migration (version) VALUES ('0002_projections');
