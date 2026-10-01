"""destinations, audience groups, server kingdom link (spec §67)

Revision ID: a1f0c0de0004
Revises: a1f0c0de0003
Create Date: 2026-10-01 14:30:00.000000

Additive, with a downgrade. Creates destinations, audience groups, the
per-event destination changes and the secondary-server list; gives every
Discord server a Kingdom; adds the delivery columns and the two partial
unique indexes that replace uq_delivery. Nothing is dropped except that
constraint, and `tenants.notification_*` and the `event_alliances`
overrides stay until the closing revision (§67.8).

It also seeds data so behavior is unchanged the moment it runs: one
"Notifications" destination per alliance that had a channel, and one
destination plus event_destinations rows per per-event override.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1f0c0de0004'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_server_kingdoms(bind) -> None:
    bind.execute(sa.text(
        "UPDATE discord_servers SET kingdom_id = "
        "(SELECT MIN(t.kingdom_id) FROM tenants t WHERE t.server_id = discord_servers.id) "
        "WHERE kingdom_id IS NULL"
    ))
    orphans = bind.execute(sa.text("SELECT id, name FROM discord_servers WHERE kingdom_id IS NULL")).fetchall()
    if not orphans:
        return
    kingdoms = [r[0] for r in bind.execute(sa.text("SELECT id FROM kingdoms")).fetchall()]
    if len(kingdoms) == 1:
        bind.execute(sa.text("UPDATE discord_servers SET kingdom_id = :k WHERE kingdom_id IS NULL"), {"k": kingdoms[0]})
        return
    names = ", ".join(r[1] for r in orphans)
    raise RuntimeError(
        f"Cannot decide the Kingdom of Discord server(s): {names}. Set discord_servers.kingdom_id by hand "
        "for these rows, then run the migration again."
    )


def _seed_destinations(bind) -> None:
    tenants = bind.execute(sa.text(
        "SELECT id, server_id, notification_channel_id, notification_role_id FROM tenants ORDER BY id"
    )).fetchall()
    default_of: dict[int, tuple[int, str]] = {}
    labels_used: dict[int, int] = {}
    for tid, server_id, channel, role in tenants:
        if not channel:
            continue
        dest_id = bind.execute(sa.text(
            "INSERT INTO destinations (tenant_id, server_id, channel_id, role_id, label, post_by_default, leadership_only) "
            "VALUES (:t, :s, :c, :r, 'Notifications', :yes, :no) RETURNING id"
        ), {"t": tid, "s": server_id, "c": channel, "r": role or "", "yes": True, "no": False}).scalar_one()
        default_of[tid] = (dest_id, channel)

    overrides = bind.execute(sa.text(
        "SELECT ea.event_id, ea.tenant_id, ea.notification_channel_id, ea.notification_role_id, "
        "       e.leadership_only, t.server_id, t.notification_channel_id, t.notification_role_id "
        "FROM event_alliances ea JOIN events e ON e.id = ea.event_id JOIN tenants t ON t.id = ea.tenant_id "
        "WHERE COALESCE(ea.notification_channel_id, '') <> '' OR COALESCE(ea.notification_role_id, '') <> '' "
        "ORDER BY ea.event_id, ea.tenant_id"
    )).fetchall()
    for event_id, tid, ov_channel, ov_role, leadership, server_id, def_channel, def_role in overrides:
        channel = ov_channel or def_channel or ""
        role = ov_role or def_role or ""
        if not channel:
            continue
        found = bind.execute(sa.text(
            "SELECT id FROM destinations WHERE tenant_id = :t AND server_id = :s AND channel_id = :c AND role_id = :r"
        ), {"t": tid, "s": server_id, "c": channel, "r": role}).scalar()
        if found is None:
            labels_used[tid] = labels_used.get(tid, 0) + 1
            n = labels_used[tid]
            found = bind.execute(sa.text(
                "INSERT INTO destinations (tenant_id, server_id, channel_id, role_id, label, post_by_default, leadership_only) "
                "VALUES (:t, :s, :c, :r, :l, :no, :lead) RETURNING id"
            ), {"t": tid, "s": server_id, "c": channel, "r": role,
                "l": "Event channel" if n == 1 else f"Event channel {n}", "no": False, "lead": bool(leadership)}).scalar_one()
        default = default_of.get(tid)
        if default is not None and found == default[0]:
            continue  # the override matched the alliance default: nothing changes
        bind.execute(sa.text(
            "INSERT INTO event_destinations (event_id, destination_id, included) VALUES (:e, :d, :yes) "
            "ON CONFLICT DO NOTHING"
        ), {"e": event_id, "d": found, "yes": True})
        # A channel override replaced the default; a role-only override did not.
        if default is not None and channel != default[1]:
            bind.execute(sa.text(
                "INSERT INTO event_destinations (event_id, destination_id, included) VALUES (:e, :d, :no) "
                "ON CONFLICT DO NOTHING"
            ), {"e": event_id, "d": default[0], "no": False})

    bind.execute(sa.text(
        "UPDATE deliveries SET destination_id = d.id, guild_id = s.guild_id, channel_id = d.channel_id "
        "FROM destinations d JOIN discord_servers s ON s.id = d.server_id "
        "WHERE deliveries.kind = 'reminder' AND d.tenant_id = deliveries.tenant_id AND d.label = 'Notifications'"
    ))


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column('discord_servers', sa.Column('kingdom_id', sa.Integer(), nullable=True))
    _backfill_server_kingdoms(bind)
    op.alter_column('discord_servers', 'kingdom_id', nullable=False)
    op.create_foreign_key('discord_servers_kingdom_id_fkey', 'discord_servers', 'kingdoms', ['kingdom_id'], ['id'])

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
    op.create_table('tenant_secondary_servers',
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['server_id'], ['discord_servers.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id', 'server_id'),
    )

    op.add_column('deliveries', sa.Column('destination_id', sa.Integer(), nullable=True))
    op.add_column('deliveries', sa.Column('guild_id', sa.Text(), server_default='', nullable=False))
    op.add_column('deliveries', sa.Column('channel_id', sa.Text(), server_default='', nullable=False))
    op.add_column('deliveries', sa.Column('merged_into_id', sa.Integer(), nullable=True))
    op.create_foreign_key('deliveries_destination_id_fkey', 'deliveries', 'destinations', ['destination_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('deliveries_merged_into_id_fkey', 'deliveries', 'deliveries', ['merged_into_id'], ['id'], ondelete='SET NULL')
    op.drop_constraint('uq_delivery', 'deliveries', type_='unique')
    op.create_index('uq_delivery_reminder', 'deliveries', ['occurrence_id', 'destination_id', 'reminder_minutes'],
                    unique=True, postgresql_where=sa.text("kind = 'reminder'"))
    op.create_index('uq_delivery_event', 'deliveries', ['occurrence_id', 'tenant_id'],
                    unique=True, postgresql_where=sa.text("kind = 'discord_event'"))

    _seed_destinations(bind)


def downgrade() -> None:
    """Reverses the additive parts. Deliveries beyond the first reminder per
    (occurrence, alliance, offset) are deleted, because the old unique
    constraint allows only one."""
    op.drop_index('uq_delivery_event', table_name='deliveries')
    op.drop_index('uq_delivery_reminder', table_name='deliveries')
    op.execute(
        "DELETE FROM deliveries WHERE id NOT IN ("
        "SELECT MIN(id) FROM deliveries GROUP BY occurrence_id, tenant_id, kind, reminder_minutes)"
    )
    op.create_unique_constraint('uq_delivery', 'deliveries', ['occurrence_id', 'tenant_id', 'kind', 'reminder_minutes'])
    op.drop_constraint('deliveries_merged_into_id_fkey', 'deliveries', type_='foreignkey')
    op.drop_constraint('deliveries_destination_id_fkey', 'deliveries', type_='foreignkey')
    op.drop_column('deliveries', 'merged_into_id')
    op.drop_column('deliveries', 'channel_id')
    op.drop_column('deliveries', 'guild_id')
    op.drop_column('deliveries', 'destination_id')
    op.drop_table('tenant_secondary_servers')
    op.drop_table('event_groups')
    op.drop_table('event_destinations')
    op.drop_table('audience_group_destinations')
    op.drop_table('audience_groups')
    op.drop_table('destinations')
    op.drop_constraint('discord_servers_kingdom_id_fkey', 'discord_servers', type_='foreignkey')
    op.drop_column('discord_servers', 'kingdom_id')
