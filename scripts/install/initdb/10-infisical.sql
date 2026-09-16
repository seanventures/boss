-- A second database on the Postgres BOSS already runs, so bundling a secret store costs one
-- container rather than two. Runs only on FIRST init of the data volume, which is why it is
-- written to be harmless when Infisical is never enabled: an empty database costs nothing.
SELECT 'CREATE DATABASE infisical OWNER boss'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'infisical')\gexec
