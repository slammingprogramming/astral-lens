-- 0012: dense retrieval. text_embedding holds one vector per (chunk, model). It is an INDEX over text
-- chunks, derived and rebuildable, never evidence. It is removed with the case. Vectors live in a plain
-- real[] and are ranked exactly in-process (the same approach as faces), so the small tier needs no
-- extension; a dedicated vector service can serve the same role at the cluster tier.

CREATE TABLE text_embedding (
  chunk_id  bigint NOT NULL,
  case_id   uuid NOT NULL,
  model_id  text NOT NULL,
  dim       int NOT NULL,
  embedding real[] NOT NULL,
  PRIMARY KEY (chunk_id, model_id)
);
CREATE INDEX text_embedding_case ON text_embedding (case_id, model_id);

INSERT INTO schema_migration (version) VALUES ('0012_dense');
