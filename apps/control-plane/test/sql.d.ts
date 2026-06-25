// Lets tests import a .sql file's text via vite's `?raw` suffix (used to apply the D1 migration to
// the better-sqlite3 test database), with no @types/node dependency.
declare module "*.sql?raw" {
  const content: string;
  export default content;
}
