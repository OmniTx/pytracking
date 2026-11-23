-- SPDX-License-Identifier: Apache-2.0
-- pytracking - Phase 3 schema: destinations and the delivery queue.
-- Idempotent: every CREATE uses IF NOT EXISTS, so re-running is a no-op.
--
-- Deliveries are materialised by the forwarder, not by the receiver, so the
-- ingest path stays as fast as Phase 1 accepted it (decision D25).

CREATE TABLE IF NOT EXISTS destinations (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id   BIGINT UNSIGNED NOT NULL,
    type        ENUM('meta', 'ga4', 'tiktok') NOT NULL,
    name        VARCHAR(128) NOT NULL DEFAULT 'default',
    -- Provider configuration: endpoint identifiers and credentials. Stored in
    -- clear for now; encryption at rest is deferred (decision D25).
    config_json JSON NOT NULL,
    active      TINYINT(1) NOT NULL DEFAULT 1,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_tenant_type_name (tenant_id, type, name),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS deliveries (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id        BIGINT UNSIGNED NOT NULL,
    -- events.id (the row id), not the client-supplied event_id string.
    event_row_id     BIGINT UNSIGNED NOT NULL,
    destination_id   BIGINT UNSIGNED NOT NULL,
    status           ENUM('pending', 'processing', 'delivered', 'dead')
                     NOT NULL DEFAULT 'pending',
    attempts         INT NOT NULL DEFAULT 0,
    next_attempt_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_until     DATETIME NULL,
    claim_token      CHAR(32) NULL,
    last_status_code INT NULL,
    last_error       VARCHAR(500) NULL,
    delivered_at     DATETIME NULL,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                     ON UPDATE CURRENT_TIMESTAMP,
    -- Makes materialisation idempotent: one delivery per event per destination.
    UNIQUE KEY uk_event_destination (event_row_id, destination_id),
    INDEX idx_due (status, next_attempt_at),
    INDEX idx_claim (claim_token),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (event_row_id) REFERENCES events(id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (destination_id) REFERENCES destinations(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
