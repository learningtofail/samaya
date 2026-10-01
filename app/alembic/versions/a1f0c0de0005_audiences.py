"""audiences: Kingdom-owned, multi-destination (spec §68)

Revision ID: a1f0c0de0005
Revises: a1f0c0de0004
Create Date: 2026-10-01 20:30:00.000000

What §67 called a destination (one alliance's channel) becomes an Audience
owned by the Kingdom, holding one or more destinations (server, channel,
role). Alliances use an Audience through `alliance_audiences`.

Upgrade:
- Old destinations with the same Kingdom, server, channel and role merge into
  one Audience (the lowest id's label and flag) with one destination. Each
  former owner becomes a link carrying its old `post_by_default`.
- Each audience group becomes an Audience holding the distinct channels of its
  members (split in two when it mixed leadership-only and ordinary members).
  It is linked, as optional, to the owner alliance of each event that used the
  group, and `event_groups` become `event_audiences` rows with included = true.
- `event_destinations` and `deliveries` follow the merge; an opt-out beats an
  add when two merged rows disagree.

Downgrade restores §67's shape: one destination per (channel, linked
alliance). Audience groups are NOT rebuilt (an Audience that came from a group
returns as plain destinations), and a delivery whose alliance has no copy of
its destination is pointed at the first copy.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1f0c0de0005'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class _Labels:
    """Hands out Audience labels that are unique within a Kingdom."""

    def __init__(self) -> None:
        self._used: set[tuple[int, str]] = set()

    def take(self, kingdom_id: int, *candidates: str) -> str:
        for candidate in candidates:
            if (kingdom_id, candidate) not in self._used:
                self._used.add((kingdom_id, candidate))
                return candidate
        n = 2
        while (kingdom_id, f"{candidates[0]} #{n}") in self._used:
            n += 1
        label = f"{candidates[0]} #{n}"
        self._used.add((kingdom_id, label))
        return label


def _insert(bind, sql: str, **params) -> int:
    return bind.execute(sa.text(sql + " RETURNING id"), params).scalar_one()


def _create_new_tables() -> None:
    op.create_table('audiences',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kingdom_id', sa.Integer(), nullable=False),
        sa.Column('label', sa.Text(), nullable=False),
        sa.Column('leadership_only', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['kingdom_id'], ['kingdoms.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kingdom_id', 'label', name='uq_audience_label'),
    )
    op.create_table('audience_destinations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('audience_id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('channel_id', sa.Text(), nullable=False),
        sa.Column('role_id', sa.Text(), server_default='', nullable=False),
        sa.CheckConstraint("channel_id <> ''", name='ck_audience_destination_channel_set'),
        sa.ForeignKeyConstraint(['audience_id'], ['audiences.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['server_id'], ['discord_servers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('audience_id', 'server_id', 'channel_id', 'role_id', name='uq_audience_destination'),
    )
    op.create_table('alliance_audiences',
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('audience_id', sa.Integer(), nullable=False),
        sa.Column('post_by_default', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['audience_id'], ['audiences.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id', 'audience_id'),
    )
    op.create_table('event_audiences',
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('audience_id', sa.Integer(), nullable=False),
        sa.Column('included', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['audience_id'], ['audiences.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('event_id', 'audience_id'),
    )


def _convert_destinations(bind, labels: _Labels) -> tuple[dict[int, int], dict[int, int]]:
    """Returns (old destination id -> audience id, old destination id ->
    audience_destinations id)."""
    rows = bind.execute(sa.text(
        "SELECT d.id, t.kingdom_id, d.server_id, d.channel_id, d.role_id, d.label, d.leadership_only, "
        "       d.tenant_id, d.post_by_default, t.name "
        "FROM destinations d JOIN tenants t ON t.id = d.tenant_id ORDER BY d.id"
    )).fetchall()
    audience_of_key: dict[tuple, tuple[int, int]] = {}
    to_audience: dict[int, int] = {}
    to_destination: dict[int, int] = {}
    for dest_id, kingdom_id, server_id, channel_id, role_id, label, leadership, tenant_id, default, tenant_name in rows:
        key = (kingdom_id, server_id, channel_id, role_id)
        if key not in audience_of_key:
            audience_id = _insert(
                bind, "INSERT INTO audiences (kingdom_id, label, leadership_only) VALUES (:k, :l, :lead)",
                k=kingdom_id, l=labels.take(kingdom_id, label, f"{label} ({tenant_name})", f"{label} #{dest_id}"),
                lead=leadership,
            )
            ad_id = _insert(
                bind, "INSERT INTO audience_destinations (audience_id, server_id, channel_id, role_id) "
                      "VALUES (:a, :s, :c, :r)", a=audience_id, s=server_id, c=channel_id, r=role_id,
            )
            audience_of_key[key] = (audience_id, ad_id)
        audience_id, ad_id = audience_of_key[key]
        to_audience[dest_id], to_destination[dest_id] = audience_id, ad_id
        bind.execute(sa.text(
            "INSERT INTO alliance_audiences (tenant_id, audience_id, post_by_default) VALUES (:t, :a, :p) "
            "ON CONFLICT (tenant_id, audience_id) DO UPDATE "
            "SET post_by_default = alliance_audiences.post_by_default OR EXCLUDED.post_by_default"
        ), {"t": tenant_id, "a": audience_id, "p": default})
    return to_audience, to_destination


def _convert_event_changes(bind, to_audience: dict[int, int]) -> None:
    rows = bind.execute(sa.text("SELECT event_id, destination_id, included FROM event_destinations")).fetchall()
    merged: dict[tuple[int, int], bool] = {}
    for event_id, dest_id, included in rows:
        key = (event_id, to_audience[dest_id])
        merged[key] = merged.get(key, True) and included  # an opt-out beats an add
    for (event_id, audience_id), included in merged.items():
        bind.execute(sa.text("INSERT INTO event_audiences (event_id, audience_id, included) VALUES (:e, :a, :i)"),
                     {"e": event_id, "a": audience_id, "i": included})


def _convert_groups(bind, labels: _Labels) -> None:
    groups = bind.execute(sa.text("SELECT id, kingdom_id, name FROM audience_groups ORDER BY id")).fetchall()
    for group_id, kingdom_id, name in groups:
        members = bind.execute(sa.text(
            "SELECT DISTINCT d.server_id, d.channel_id, d.role_id, d.leadership_only "
            "FROM audience_group_destinations g JOIN destinations d ON d.id = g.destination_id "
            "WHERE g.group_id = :g ORDER BY d.server_id, d.channel_id, d.role_id"
        ), {"g": group_id}).fetchall()
        flags = sorted({m[3] for m in members})
        event_rows = bind.execute(sa.text(
            "SELECT eg.event_id, e.owning_tenant_id FROM event_groups eg JOIN events e ON e.id = eg.event_id "
            "WHERE eg.group_id = :g"), {"g": group_id}).fetchall()
        for flag in flags:
            suffix = " (leadership)" if len(flags) > 1 and flag else ""
            audience_id = _insert(
                bind, "INSERT INTO audiences (kingdom_id, label, leadership_only) VALUES (:k, :l, :lead)",
                k=kingdom_id, l=labels.take(kingdom_id, f"{name}{suffix}", f"{name}{suffix} (group)"), lead=flag,
            )
            for server_id, channel_id, role_id, member_flag in members:
                if member_flag == flag:
                    bind.execute(sa.text(
                        "INSERT INTO audience_destinations (audience_id, server_id, channel_id, role_id) "
                        "VALUES (:a, :s, :c, :r)"), {"a": audience_id, "s": server_id, "c": channel_id, "r": role_id})
            for event_id, owner_id in event_rows:
                bind.execute(sa.text(
                    "INSERT INTO event_audiences (event_id, audience_id, included) VALUES (:e, :a, true) "
                    "ON CONFLICT DO NOTHING"), {"e": event_id, "a": audience_id})
                bind.execute(sa.text(
                    "INSERT INTO alliance_audiences (tenant_id, audience_id, post_by_default) VALUES (:t, :a, false) "
                    "ON CONFLICT DO NOTHING"), {"t": owner_id, "a": audience_id})


def upgrade() -> None:
    bind = op.get_bind()
    _create_new_tables()
    labels = _Labels()
    to_audience, to_destination = _convert_destinations(bind, labels)
    _convert_event_changes(bind, to_audience)
    _convert_groups(bind, labels)

    # Deliveries: a new column avoids clashes between the old and new id spaces.
    op.drop_index('uq_delivery_reminder', table_name='deliveries')
    op.add_column('deliveries', sa.Column('audience_destination_id', sa.Integer(), nullable=True))
    for old_id, new_id in to_destination.items():
        bind.execute(sa.text("UPDATE deliveries SET audience_destination_id = :n WHERE destination_id = :o"),
                     {"n": new_id, "o": old_id})
    op.drop_column('deliveries', 'destination_id')
    op.alter_column('deliveries', 'audience_destination_id', new_column_name='destination_id')
    op.create_foreign_key('deliveries_destination_id_fkey', 'deliveries', 'audience_destinations',
                          ['destination_id'], ['id'], ondelete='SET NULL')

    op.drop_table('event_groups')
    op.drop_table('audience_group_destinations')
    op.drop_table('audience_groups')
    op.drop_table('event_destinations')
    op.drop_table('destinations')
    op.create_index('uq_delivery_reminder', 'deliveries', ['occurrence_id', 'destination_id', 'tenant_id', 'reminder_minutes'],
                    unique=True, postgresql_where=sa.text("kind = 'reminder'"))


def _create_old_tables() -> None:
    op.create_table('destinations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('channel_id', sa.Text(), nullable=False),
        sa.Column('role_id', sa.Text(), server_default='', nullable=False),
        sa.Column('label', sa.Text(), nullable=False),
        sa.Column('post_by_default', sa.Boolean(), nullable=False),
        sa.Column('leadership_only', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint("channel_id <> ''", name='ck_destination_channel_set'),
        sa.ForeignKeyConstraint(['server_id'], ['discord_servers.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'label', name='uq_destination_label'),
        sa.UniqueConstraint('tenant_id', 'server_id', 'channel_id', 'role_id', name='uq_destination_target'),
    )
    op.create_table('audience_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kingdom_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['kingdom_id'], ['kingdoms.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kingdom_id', 'name', name='uq_audience_group_name'),
    )
    op.create_table('audience_group_destinations',
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('destination_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['destination_id'], ['destinations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['group_id'], ['audience_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('group_id', 'destination_id'),
    )
    op.create_table('event_destinations',
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('destination_id', sa.Integer(), nullable=False),
        sa.Column('included', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['destination_id'], ['destinations.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('event_id', 'destination_id'),
    )
    op.create_table('event_groups',
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['audience_groups.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('event_id', 'group_id'),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index('uq_delivery_reminder', table_name='deliveries')
    _create_old_tables()

    # One old destination per (alliance, server, channel, role).
    used_labels: set[tuple[int, str]] = set()
    by_target: dict[tuple, int] = {}
    copies_of: dict[int, dict[int, int]] = {}      # audience_destination id -> {tenant id: old id}
    copies_of_audience: dict[int, list[int]] = {}  # audience id -> old ids
    audiences = bind.execute(sa.text("SELECT id, kingdom_id, label, leadership_only FROM audiences ORDER BY id")).fetchall()
    for audience_id, kingdom_id, label, leadership in audiences:
        links = bind.execute(sa.text(
            "SELECT a.tenant_id, a.post_by_default FROM alliance_audiences a WHERE a.audience_id = :a ORDER BY a.tenant_id"
        ), {"a": audience_id}).fetchall()
        if not links:  # unlinked: give it to the Kingdom's first alliance so the old schema can hold it
            first = bind.execute(sa.text("SELECT id FROM tenants WHERE kingdom_id = :k ORDER BY id LIMIT 1"),
                                 {"k": kingdom_id}).scalar()
            links = [(first, False)]
        dests = bind.execute(sa.text(
            "SELECT id, server_id, channel_id, role_id FROM audience_destinations WHERE audience_id = :a ORDER BY id"
        ), {"a": audience_id}).fetchall()
        for index, (ad_id, server_id, channel_id, role_id) in enumerate(dests):
            for tenant_id, default in links:
                key = (tenant_id, server_id, channel_id, role_id)
                if key not in by_target:
                    base = label if index == 0 else f"{label} ({index + 1})"
                    candidate, n = base, 2
                    while (tenant_id, candidate) in used_labels:
                        candidate, n = f"{base} #{n}", n + 1
                    used_labels.add((tenant_id, candidate))
                    by_target[key] = _insert(
                        bind,
                        "INSERT INTO destinations (tenant_id, server_id, channel_id, role_id, label, post_by_default, "
                        "leadership_only) VALUES (:t, :s, :c, :r, :l, :p, :lead)",
                        t=tenant_id, s=server_id, c=channel_id, r=role_id, l=candidate, p=default, lead=leadership,
                    )
                copies_of.setdefault(ad_id, {})[tenant_id] = by_target[key]
                copies_of_audience.setdefault(audience_id, []).append(by_target[key])

    for event_id, audience_id, included in bind.execute(
        sa.text("SELECT event_id, audience_id, included FROM event_audiences")
    ).fetchall():
        for old_id in dict.fromkeys(copies_of_audience.get(audience_id, [])):
            bind.execute(sa.text(
                "INSERT INTO event_destinations (event_id, destination_id, included) VALUES (:e, :d, :i) "
                "ON CONFLICT DO NOTHING"), {"e": event_id, "d": old_id, "i": included})

    op.add_column('deliveries', sa.Column('old_destination_id', sa.Integer(), nullable=True))
    for delivery_id, ad_id, tenant_id in bind.execute(sa.text(
        "SELECT id, destination_id, tenant_id FROM deliveries WHERE destination_id IS NOT NULL"
    )).fetchall():
        copies = copies_of.get(ad_id)
        if not copies:
            continue
        bind.execute(sa.text("UPDATE deliveries SET old_destination_id = :o WHERE id = :i"),
                     {"o": copies.get(tenant_id, next(iter(copies.values()))), "i": delivery_id})
    op.drop_column('deliveries', 'destination_id')
    op.alter_column('deliveries', 'old_destination_id', new_column_name='destination_id')
    op.create_foreign_key('deliveries_destination_id_fkey', 'deliveries', 'destinations',
                          ['destination_id'], ['id'], ondelete='SET NULL')

    op.drop_table('event_audiences')
    op.drop_table('alliance_audiences')
    op.drop_table('audience_destinations')
    op.drop_table('audiences')
    op.execute(
        "DELETE FROM deliveries WHERE kind = 'reminder' AND id NOT IN ("
        "SELECT MIN(id) FROM deliveries WHERE kind = 'reminder' GROUP BY occurrence_id, destination_id, reminder_minutes)"
    )
    op.create_index('uq_delivery_reminder', 'deliveries', ['occurrence_id', 'destination_id', 'reminder_minutes'],
                    unique=True, postgresql_where=sa.text("kind = 'reminder'"))
