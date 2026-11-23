# SPDX-License-Identifier: Apache-2.0
"""MySQL-backed store for the forwarder.

Every method is one short statement so a run holds no long transaction. The
claim is an ``UPDATE ... ORDER BY ... LIMIT``: InnoDB locks the rows it
touches, so two concurrent runs cannot take the same delivery, and a run that
dies leaves ``locked_until`` in the past so the rows come back.
"""

import json
import secrets


class MySQLStore:
    def __init__(self, connection):
        self.connection = connection

    # -- queue ------------------------------------------------------------

    def materialise(self, limit):
        """Create pending deliveries for events that lack one. Idempotent."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT IGNORE INTO deliveries "
                "(tenant_id, event_row_id, destination_id, status, next_attempt_at) "
                "SELECT e.tenant_id, e.id, d.id, 'pending', NOW() "
                "FROM events e "
                "JOIN destinations d "
                "  ON d.tenant_id = e.tenant_id AND d.active = 1 "
                "LEFT JOIN deliveries dl "
                "  ON dl.event_row_id = e.id AND dl.destination_id = d.id "
                "WHERE dl.id IS NULL "
                "LIMIT %s",
                (limit,),
            )
            inserted = cursor.rowcount
        self.connection.commit()
        return inserted

    def claim(self, limit, lock_seconds):
        """Take up to *limit* due deliveries. Returns ``(token, rows)``."""
        token = secrets.token_hex(16)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deliveries "
                "SET status = 'processing', claim_token = %s, "
                "    locked_until = DATE_ADD(NOW(), INTERVAL %s SECOND), "
                "    attempts = attempts + 1 "
                "WHERE status IN ('pending', 'processing') "
                "  AND next_attempt_at <= NOW() "
                "  AND (locked_until IS NULL OR locked_until < NOW()) "
                "ORDER BY next_attempt_at, id "
                "LIMIT %s",
                (token, lock_seconds, limit),
            )
            cursor.execute(
                "SELECT id, tenant_id, event_row_id, destination_id, attempts "
                "FROM deliveries WHERE claim_token = %s ORDER BY id",
                (token,),
            )
            rows = [
                {
                    "id": row[0],
                    "tenant_id": row[1],
                    "event_row_id": row[2],
                    "destination_id": row[3],
                    "attempts": row[4],
                }
                for row in cursor.fetchall()
            ]
        self.connection.commit()
        return token, rows

    # -- reads ------------------------------------------------------------

    def load_event(self, event_row_id):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, tenant_id, event_id, name, received_at, payload_json, "
                "       user_agent, source_origin, consent_state "
                "FROM events WHERE id = %s",
                (event_row_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        received_at = row[4]
        payload = {}
        if row[5]:
            try:
                payload = json.loads(row[5])
            except (TypeError, ValueError):
                payload = {}
        return {
            "row_id": row[0],
            "tenant_id": row[1],
            "event_id": row[2],
            "name": row[3],
            "received_at": received_at,
            "received_at_unix": int(received_at.timestamp()) if received_at else 0,
            "payload": payload if isinstance(payload, dict) else {},
            "user_agent": row[6],
            "source_origin": row[7],
            "consent_state": row[8],
        }

    def load_destination(self, destination_id):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, tenant_id, type, config_json FROM destinations "
                "WHERE id = %s AND active = 1",
                (destination_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        try:
            config = json.loads(row[3]) if row[3] else {}
        except (TypeError, ValueError):
            config = {}
        return {
            "id": row[0],
            "tenant_id": row[1],
            "type": row[2],
            "config": config if isinstance(config, dict) else {},
        }

    # -- outcomes ---------------------------------------------------------

    def mark_delivered(self, delivery_id, status_code):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deliveries SET status = 'delivered', "
                "    delivered_at = NOW(), last_status_code = %s, last_error = NULL, "
                "    claim_token = NULL, locked_until = NULL "
                "WHERE id = %s",
                (status_code, delivery_id),
            )
        self.connection.commit()

    def mark_retry(self, delivery_id, delay_seconds, error, status_code):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deliveries SET status = 'pending', "
                "    next_attempt_at = DATE_ADD(NOW(), INTERVAL %s SECOND), "
                "    last_error = %s, last_status_code = %s, "
                "    claim_token = NULL, locked_until = NULL "
                "WHERE id = %s",
                (int(delay_seconds), error, status_code, delivery_id),
            )
        self.connection.commit()

    def mark_dead(self, delivery_id, error, status_code):
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE deliveries SET status = 'dead', last_error = %s, "
                "    last_status_code = %s, claim_token = NULL, locked_until = NULL "
                "WHERE id = %s",
                (error, status_code, delivery_id),
            )
        self.connection.commit()
