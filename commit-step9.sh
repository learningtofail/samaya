#!/bin/bash
set -euo pipefail
cd /opt/taraka

git status --short

git add app/static/admin.html app/static/events.html

git commit -F - << 'COMMIT_MSG'
Apply Twilight Slate theme and Step 6 terminology/badge rename

Step 5 (previously uncommitted):
- Replace warm-beige palette with Twilight Slate across :root
  variables (--bg, --banner, --accent, --green, --red, --amber, --blue)
- Add rem-based type scale (--fs-2xs through --fs-lg) with 16px root
  font-size and 1.5 line-height baseline
- Add 12-palette theme switcher with cookie persistence and
  synchronous head-script to prevent flash-of-wrong-theme on load
- Fix WCAG contrast on .btn-accent (was 1:1 for 11 of 12 palettes)

Terminology and badge rename (this session):
- Rename "Community" to "Alliance" throughout admin.html: section
  headers (Events/Schedule/Gantt), postSelected() scope string and
  matching data-scope value, empty-state copy
- Rename "Leadership Events" to "Leadership Notifications" in section
  headers
- Replace lock icon with crown for leadership items; add shield icon
  as the Alliance-side counterpart (Dashboard, Schedule row, Post Log
  row, Leadership Only checkbox label)

No schema or backend changes. No new test coverage added for either
pass - admin.html has no existing frontend test infrastructure.
COMMIT_MSG

git push origin master

echo "--- done ---"
git log -1 --oneline
