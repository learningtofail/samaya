# Rollback Runbook

Audit remediation, Phase 5. There was previously no written procedure for
undoing a bad deploy on `lxc-taraka` — this fills that gap. It does not
replace judgment: read the situation, then pick the matching section below.

Every command here runs on `lxc-taraka`, inside `/opt/taraka` (this repo's
deploy checkout), unless a step says otherwise.

## Which kind of rollback do you need?

- **Only the application code is bad** (a router bug, a bad template, a
  frontend regression) and the database schema hasn't changed since the
  last good deploy → [Code-only rollback](#code-only-rollback). Fastest,
  safest, no data loss risk.
- **The last deploy included an Alembic migration** and that migration (or
  the code that shipped with it) is the problem → [Rolling back a
  migration](#rolling-back-a-migration). Read that section fully before
  running anything — whether `alembic downgrade` is even safe depends on
  what the migration did.
- **Data is wrong or missing** (not just "the code is buggy," but actual
  rows were corrupted, deleted, or never should have been written) →
  [Restoring from backup](#restoring-from-backup). Last resort — it loses
  every write made since the backup being restored.

## Code-only rollback

Use this when the currently-deployed schema (whatever `alembic current`
reports) is still correct for the previous, known-good commit — i.e. the
last deploy didn't run a new migration, or the migration itself is fine
and only the application code needs to go back.

    cd /opt/taraka
    git log --oneline -5            # find the last known-good commit
    git checkout <good-commit-sha>  # detached HEAD is fine for this
    docker compose up --build -d app
    curl -I http://127.0.0.1:8000/health

Verify the rollback actually took (the built image, not just the checkout,
needs to reflect the old commit):

    docker compose exec app python -c "import subprocess; print('rolled back')"
    docker compose logs app --tail 50

Once confirmed and the underlying issue is understood, return to `master`
in the normal way (fix forward, don't leave the deploy checkout on a
detached HEAD indefinitely):

    git checkout master

## Rolling back a migration

**First, decide whether `alembic downgrade` is actually safe.** Not every
migration has a safe automatic downgrade:

- A migration that only **added** a table, a column with a default, or a
  nullable column → downgrading (dropping it) is safe as long as nothing
  written to that column since the migration ran is worth keeping.
- A migration that **dropped** a column or table, **renamed** something, or
  **widened/narrowed a constraint** (e.g. `migrate_add_viewer_role.py`'s
  `ck_user_tenant_role` widening, or any future Alembic-generated
  `ALTER COLUMN`) → the generated `downgrade()` may not restore dropped
  data at all (a dropped column's data is gone the moment `upgrade()` ran,
  regardless of what `downgrade()` does afterward). Check the specific
  revision file under `app/alembic/versions/` before running anything —
  read its `downgrade()` function, not just its name.

If the answer is "yes, downgrade is safe":

    docker compose exec app alembic history          # find the revision to go back to
    docker compose exec app alembic downgrade <revision>
    # then also roll back the application code (previous section) if the
    # code that shipped with this migration is also part of the problem

If the answer is "no, or I'm not sure" — stop, and go to
[Restoring from backup](#restoring-from-backup) instead. A migration whose
downgrade can't restore what `upgrade()` destroyed is not something to
guess about on a live database; restoring the pre-migration backup and
replaying anything worth keeping by hand is slower but doesn't compound
the mistake.

### Revision `a1f0c0de0004` (destinations, groups, secondary servers)

Additive, so `alembic downgrade a1f0c0de0003` is safe: it drops the new tables
and delivery columns and restores the old unique constraint. Destinations,
groups and secondary servers created since are lost, and the old per-event
channel overrides are not restored (they were never deleted from
`event_alliances`, so the old code still reads them). Revision `0005`, which
drops those legacy columns, is destructive and has no downgrade: take a backup
first.

## Restoring from backup

`ops/backup.sh` runs nightly (03:00 server time, see its own header for
the one-time `crontab` install) and writes gzipped `pg_dump` output to
`/opt/taraka/backups/kingshot_scheduler_<timestamp>.sql.gz`, keeping the
last 14 days. This restores the **entire database** to that backup's
point in time — every write since then (new events, announcements,
tickets, votes, anything) is lost. Confirm that's actually an acceptable
trade-off before running this.

    ls -la /opt/taraka/backups/                # find the backup to restore
    cd /opt/taraka
    docker compose stop app                    # stop writes before restoring under it

    # Drop and recreate the database, then restore into it:
    docker compose exec db psql -U taraka -c "DROP DATABASE kingshot_scheduler;"
    docker compose exec db psql -U taraka -c "CREATE DATABASE kingshot_scheduler;"
    gunzip -c /opt/taraka/backups/kingshot_scheduler_<timestamp>.sql.gz \
      | docker compose exec -T db psql -U taraka -d kingshot_scheduler

    # The restored dump reflects whatever schema existed when it was taken.
    # If code has since shipped with a migration past that point, bring the
    # schema forward again (safe to do — replaying migrations that add
    # things back is not the same problem as downgrading one that already
    # destroyed data):
    docker compose exec app alembic upgrade head

    docker compose start app
    curl -I http://127.0.0.1:8000/health

## Unified event model cutover

The cutover (`docs/cutover-runbook.md`) has two points of no return, and the
rollback differs by which one you have passed.

- **Before step 7 (`alembic upgrade head`) ran:** the database is unchanged.
  Check out the previous commit, rebuild, start the app:

      git checkout <previous-commit-sha>
      docker compose up --build -d app

  Any Discord events removed in step 6 are gone; the old system recreates
  them on its next auto-post pass (daily, 16:00 UTC).

- **After step 7 ran:** the old tables are dropped and `alembic downgrade`
  refuses to run. Restore the step 1 backup using "Restoring from backup"
  above, check out the previous commit, and rebuild. Do not run `alembic
  upgrade head` after restoring: the restored database is at the old head and
  must stay there while the old code runs. Everything created in the new
  console after the cutover is lost with the restore.

## After any rollback

- Check `docker compose logs app --tail 100` for startup errors — this
  app refuses to boot without `SECRET_KEY`/Discord OAuth credentials (see
  `main.py`'s own startup check), so a config issue surfaces immediately
  rather than as a silent partial failure.
- Confirm `alembic current` (inside the `app` container) matches what the
  running code actually expects — a code/schema mismatch (running code
  that assumes a column exists, against a database that was rolled back
  past the migration that added it) is the single most common way a
  rollback makes things worse instead of better.
- Write down what actually happened and why, somewhere a future rollback
  can find it — this file, or a note in the PR/commit that caused the
  need for one.
