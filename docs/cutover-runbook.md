# Cutover runbook: unified event model (spec §66)

One-time procedure for moving `lxc-taraka` from the old event/announcement
system to the unified event model. It is destructive: the old events,
announcements, occurrences and post log are dropped, and the new console
starts with no events. Run it only when you are ready to re-enter the events.
Every command runs on `lxc-taraka` in `/opt/taraka` unless a step says otherwise.
Run one step at a time and check its output before the next.

## Before you start

- The branch is merged to `master` and CI is green on the merge commit.
- You have the list of events and announcements to re-create (step 3 exports
  a reference copy).
- Each alliance's Notifications channel (and role, if used) is known. The new
  engine sends reminders there; it has no per-event default any more.
- `SAMAYA_UNIFIED_ENGINE` is gone. If it is set in `.env`, delete the line;
  the new engine is the only engine and always runs.

## 1. Back up and prove the backup is readable

    ./ops/backup.sh
    ls -la /opt/taraka/backups/ | tail -3
    gunzip -c /opt/taraka/backups/<newest-file>.sql.gz | head -5

RISK: this backup is the only rollback for the schema change. Do not continue
without a fresh file whose first lines are SQL.

## 2. Check the migration state

    docker compose exec app alembic current

It must print `6fc935931248`. If it prints nothing, the Alembic baseline was
never adopted: stop and follow the baseline note in `README.md` first.

## 3. Export a reference copy of what will be dropped

    docker compose exec db psql -U taraka -d kingshot_scheduler -c "\copy (select e.id, t.slug as alliance, e.scope, e.name, e.interval_days, e.start_time_utc, e.duration_hours, e.anchor_date, e.leadership_only, e.active, e.notification_channel_id, e.description from event_definitions e join tenants t on t.id = e.owning_tenant_id order by e.id) to '/tmp/old_events.csv' csv header"
    docker compose exec db psql -U taraka -d kingshot_scheduler -c "\copy (select a.id, t.slug as alliance, a.title, a.body_markdown, a.scheduled_for, a.status, a.recurring, a.interval_days, a.leadership_only from announcements a join tenants t on t.id = a.owning_tenant_id order by a.id) to '/tmp/old_announcements.csv' csv header"
    docker compose cp db:/tmp/old_events.csv ./old_events.csv
    docker compose cp db:/tmp/old_announcements.csv ./old_announcements.csv

If a column name is rejected, the old schema differs from this checkout:
read the error, adjust the column list, and re-run. These files are for you
to read while re-entering events; nothing imports them.

## 4. Stop the old app

    docker compose stop app

The old scheduler stops posting. The public site is down until step 8.

## 5. Get the new code and build the image

    git pull --ff-only origin master
    docker compose build app

## 6. Delete the Discord events the old system created

Dry run first. It lists every Discord Scheduled Event recorded in the old
post log, one line each, and changes nothing:

    docker compose run --rm app python cutover_delete_old_discord_events.py

Check that the list is only events Samaya made. Events created by hand in
Discord are not in the post log and are never touched. Then:

    docker compose run --rm app python cutover_delete_old_discord_events.py --apply

Every line must say `deleted`. A failure line (for example `403 Forbidden`
from a bot that lost its permission) leaves that ID in the post log, and the
migration in step 7 refuses to run until it is cleared. Fix the cause and
re-run the same command; it is safe to repeat.

## 7. Apply the schema change

    docker compose run --rm app alembic upgrade head
    docker compose run --rm app alembic current
    docker compose run --rm app alembic check

`current` must print `a1f0c0de0003 (head)` and `check` must print `No new
upgrade operations detected.`

RISK: this drops `event_definitions`, `occurrences`, `post_log`,
`event_targets`, `event_tenant_notifications`, `announcements`,
`announcement_targets`, `announcement_templates` and `scheduler_state`.
There is no downgrade; rollback is the step 1 backup (see
`docs/rollback-runbook.md`, "Unified event model cutover").

## 8. Start the new app

    docker compose up -d app
    curl -s http://127.0.0.1:8000/health
    docker compose logs app --tail 30

The log must contain `Samaya scheduler started: daily generation at UTC
00:00, delivery tick every minute`.

## 9. Configure and re-enter

In the admin console (Setup tab), open the Audiences panel. The migration
seeds a "Notifications" audience from each alliance's old channel and role
(alliances that shared a channel and role share one audience), so check each is
right, add destinations on other servers if needed, and under "Alliances and
audiences" confirm which audiences each alliance uses and posts to by default.
Then create the events from `old_events.csv` and
`old_announcements.csv`. Each Kingdom already has a "General" event type;
create the others you want in the Event types tab first.

## 10. Smoke test

1. Create an event for MOD that starts about 20 minutes from now with a
   10-minute reminder and no calendar entry (leave duration empty).
2. Open Delivery log. Within a minute of the due times the reminder row must
   show `posted` and the message must appear in the alliance channel. Rows
   stuck in `pending` after 5 minutes mean the tick is not running: check
   `docker compose logs app`.
3. Open `https://ks138.taraka.dev/events/mod`: the event is listed.
4. Delete the test event (this removes anything it posted).

Cutover is done when step 10 passes.

## If it goes wrong

See `docs/rollback-runbook.md`, "Unified event model cutover". Short version:
before step 7, start the old app again (`docker compose up -d app`) after
checking out the previous commit. After step 7, restore the step 1 backup
and run the previous commit. The Discord events deleted in step 6 are not
restored by either path; the old system recreates them on its next
auto-post pass.
