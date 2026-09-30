import { defineConfig } from "vitest/config";

// Audit remediation, Phase 4 — a minimal JS test runner for
// app/static/events-public.js's pure functions. No build step/bundler is
// introduced into the shipped app by this: vitest only runs under `npm
// test`, dev-time only, the same way pytest is dev-only for the Python
// side. See tests/frontend/events-public.test.js for how the production
// script is loaded and exposes its pure functions for testing.
export default defineConfig({
  test: {
    include: ["tests/frontend/**/*.test.js"],
  },
});
