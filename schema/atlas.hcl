// Atlas runs only in Docker: the docker-compose `migrate` service applies the migrations and
// `migrate-diff` generates them. URLs come from the environment so docker-compose.test.yml can
// point `migrate` at the test database.
env "compose" {
  // Desired state: `migrate diff` compares migrations/ against it and writes the difference.
  src = "file://schema.sql"
  url = getenv("ATLAS_URL")
  // Empty scratch database (created by docker/initdb) used to normalize schema.sql and replay
  // the migration directory before diffing.
  dev = getenv("ATLAS_DEV_URL")
  migration {
    dir = "file://migrations"
  }
}
