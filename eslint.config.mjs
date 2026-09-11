import js from "@eslint/js";
import html from "eslint-plugin-html";

export default [
  {
    files: ["app/static/*.html"],
    plugins: { html },
    languageOptions: {
      ecmaVersion: 2020,
      globals: {
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
        CAT_COLORS:    "readonly",
        GANTT_PALETTE: "readonly",
        DOW3:          "readonly",
        DOW_FULL:      "readonly",
        MONTH:         "readonly",
        STATUS_LABELS: "readonly",
      },
    },
    rules: {
      "no-undef":       "error",
      "no-unreachable": "error",
    },
  },
];
