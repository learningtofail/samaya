import js from "@eslint/js";
import html from "eslint-plugin-html";

const globals = {
  MutationObserver: "readonly",
  Image:         "readonly",
  document:      "readonly",
  window:        "readonly",
  fetch:         "readonly",
  alert:         "readonly",
  confirm:       "readonly",
  prompt:        "readonly",
  console:       "readonly",
  setTimeout:    "readonly",
  setInterval:   "readonly",
  clearInterval: "readonly",
  localStorage:  "readonly",
  Intl:          "readonly",
  URL:           "readonly",
  crypto:        "readonly",
  CSS:           "readonly",
  Event:         "readonly",
  FileReader:    "readonly",
  FormData:      "readonly",
  navigator:     "readonly",
  CAT_COLORS:    "readonly",
  GANTT_PALETTE: "readonly",
  DOW3:          "readonly",
  DOW_FULL:      "readonly",
  MONTH:         "readonly",
  STATUS_LABELS: "readonly",
};

export default [
  {
    files: ["app/static/*.html"],
    plugins: { html },
    languageOptions: { ecmaVersion: 2020, globals },
    rules: {
      "no-undef":       "error",
      "no-unreachable": "error",
    },
  },
  // Spec §62 — the public events page redesign moved its inline <script>
  // out to a standalone app/static/events-public.js, which the rule above
  // (HTML-only, via eslint-plugin-html extracting inline <script> blocks)
  // never covers on its own. Scoped to app/static/*.js (top-level only,
  // not app/static/js/*.js) so this doesn't suddenly start linting the
  // admin console's existing js/*.js files, which were never covered by
  // CI and aren't part of this change.
  {
    files: ["app/static/*.js"],
    languageOptions: { ecmaVersion: 2020, sourceType: "script", globals },
    rules: {
      "no-undef":       "error",
      "no-unreachable": "error",
    },
  },
];
