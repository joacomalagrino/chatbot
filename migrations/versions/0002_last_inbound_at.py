"""add last_inbound_at to conversations

Agrega conversations.last_inbound_at: timestamp del último mensaje entrante del
usuario. Lo usa el ruteo de WhatsApp para saber si la ventana de servicio de 24h
sigue abierta (free-form) o ya cerró (hay que mandar una plantilla aprobada).

Nullable: las conversaciones viejas (y los canales sin inbound, como Lead Ads)
quedan en NULL, lo que se interpreta como "ventana cerrada" (fail-safe → plantilla).

Revision ID: 0002_last_inbound_at
Revises: 0001_baseline
Create Date: 2026-06-26 15:48:36.794474

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# identificadores de revisión, usados por Alembic.
revision: str = '0002_last_inbound_at'
down_revision: Union[str, None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # Guardamos contra la columna ya existente: igual que database.py crea sus índices
    # con IF NOT EXISTS, una DB creada por create_all (dev/test con el modelo actual) ya
    # la tiene. Sin esto, el ADD COLUMN fallaría con "duplicate column". La adopción real
    # de prod (create_all con el modelo VIEJO + stamp baseline) sí entra acá y la crea.
    if not _has_column('conversations', 'last_inbound_at'):
        with op.batch_alter_table('conversations', schema=None) as batch_op:
            batch_op.add_column(sa.Column('last_inbound_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    if _has_column('conversations', 'last_inbound_at'):
        with op.batch_alter_table('conversations', schema=None) as batch_op:
            batch_op.drop_column('last_inbound_at')
