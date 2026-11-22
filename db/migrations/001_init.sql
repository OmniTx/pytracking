-- SPDX-License-Identifier: Apache-2.0
-- pytracking - Phase 1 schema: tenants, tenant_keys, tenant_origins, events.
-- Idempotent: every CREATE uses IF NOT EXISTS, so re-running is a no-op.
-- Tables: utf8mb4 / InnoDB throughout.

CREATE TABLE IF NOT EXISTS tenants (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    slug        VARCHAR(128) NOT NULL UNIQUE,
    status      ENUM('active', 'disabled') NOT NULL DEFAULT 'active',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tenant_keys (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id   BIGINT UNSIGNED NOT NULL,
    write_key   VARCHAR(128) NOT NULL UNIQUE,
    active      TINYINT(1) NOT NULL DEFAULT 1,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tenant_origins (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id   BIGINT UNSIGNED NOT NULL,
    origin      VARCHAR(255) NOT NULL,
    UNIQUE KEY uk_tenant_origin (tenant_id, origin),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS events (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tenant_id       BIGINT UNSIGNED NOT NULL,
    event_id        VARCHAR(64) NOT NULL,
    name            VARCHAR(64) NOT NULL,
    received_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload_json    JSON,
    consent_state   TINYINT DEFAULT NULL,
    client_ip_hash  CHAR(64) NOT NULL,
    user_agent      VARCHAR(255),
    source_origin   VARCHAR(255),
    UNIQUE KEY uk_tenant_event (tenant_id, event_id),
    INDEX idx_tenant_received (tenant_id, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
