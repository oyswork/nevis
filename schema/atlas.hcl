// Atlas runs only in Docker: the docker-compose `migrate` service applies this env.
// URLs come from the environment so docker-compose.test.yml can point it at the test database.
env "compose" {
  src = "file://schema.sql"
  url = getenv("ATLAS_URL")
  // Empty scratch database (created by docker/initdb) used to normalize schema.sql.
  dev = getenv("ATLAS_DEV_URL")
}
