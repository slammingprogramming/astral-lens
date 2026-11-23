-- 0015: built-in lenses and machines (builtin.py). They ship with the program as files and are
-- installed into a workspace only when an operator asks, so they are recorded with their own
-- origin rather than passed off as local work or as an import from someone else.

ALTER TABLE lens DROP CONSTRAINT lens_origin_check;
ALTER TABLE lens ADD CONSTRAINT lens_origin_check CHECK (origin IN ('local', 'imported', 'builtin'));
ALTER TABLE machine DROP CONSTRAINT machine_origin_check;
ALTER TABLE machine ADD CONSTRAINT machine_origin_check CHECK (origin IN ('local', 'imported', 'builtin'));
