-- =============================================================================
-- ARVP Platform - PostgreSQL Initialization Script
-- =============================================================================
-- Run order: Extensions → Tables → Indexes → Default Data
-- =============================================================================

-- ─── Extensions ───────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";       -- for fuzzy text search
CREATE EXTENSION IF NOT EXISTS "btree_gin";     -- for composite GIN indexes

-- =============================================================================
-- TABLES
-- =============================================================================

-- ─── robots ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS robots (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    robot_id        VARCHAR(64)  NOT NULL UNIQUE,
    name            VARCHAR(128) NOT NULL,
    model           VARCHAR(64)  NOT NULL DEFAULT 'AMR-v1',
    firmware_version VARCHAR(32) NOT NULL DEFAULT '0.0.0',
    status          VARCHAR(32)  NOT NULL DEFAULT 'IDLE'
                        CHECK (status IN ('IDLE','MOVING','CHARGING','FAULT','OFFLINE','MAINTENANCE')),
    simulation_run_id UUID,
    fleet_id        VARCHAR(64),
    home_position   JSONB,
    capabilities    JSONB        NOT NULL DEFAULT '{}',
    metadata        JSONB        NOT NULL DEFAULT '{}',
    last_seen_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── telemetry_events ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS telemetry_events (
    id              UUID         NOT NULL DEFAULT uuid_generate_v4(),
    robot_id        VARCHAR(64)  NOT NULL,
    sequence_num    BIGINT       NOT NULL,
    timestamp       TIMESTAMPTZ  NOT NULL,
    received_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Position & Motion
    pos_x           DOUBLE PRECISION,
    pos_y           DOUBLE PRECISION,
    pos_z           DOUBLE PRECISION,
    orientation_w   DOUBLE PRECISION,
    orientation_x   DOUBLE PRECISION,
    orientation_y   DOUBLE PRECISION,
    orientation_z   DOUBLE PRECISION,
    linear_vel_x    DOUBLE PRECISION,
    linear_vel_y    DOUBLE PRECISION,
    angular_vel_z   DOUBLE PRECISION,
    -- System Health
    battery_level   DOUBLE PRECISION CHECK (battery_level >= 0 AND battery_level <= 1),
    battery_voltage DOUBLE PRECISION,
    cpu_temp        DOUBLE PRECISION,
    cpu_usage       DOUBLE PRECISION CHECK (cpu_usage >= 0 AND cpu_usage <= 100),
    memory_usage    DOUBLE PRECISION CHECK (memory_usage >= 0 AND memory_usage <= 100),
    -- Navigation
    mission_id      VARCHAR(64),
    waypoint_index  INTEGER,
    path_efficiency DOUBLE PRECISION,
    -- Raw payload for extensibility
    raw_payload     JSONB        NOT NULL DEFAULT '{}',
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

-- Partition by month (create 3 months ahead)
CREATE TABLE IF NOT EXISTS telemetry_events_y2024m01
    PARTITION OF telemetry_events
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

CREATE TABLE IF NOT EXISTS telemetry_events_y2024m02
    PARTITION OF telemetry_events
    FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');

CREATE TABLE IF NOT EXISTS telemetry_events_default
    PARTITION OF telemetry_events DEFAULT;

-- BRIN index for time-series range scans (very efficient on append-only data)
CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp_brin
    ON telemetry_events USING BRIN (timestamp)
    WITH (pages_per_range = 128);

-- ─── simulation_runs ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS simulation_runs (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          VARCHAR(64)  NOT NULL UNIQUE,
    name            VARCHAR(256) NOT NULL,
    description     TEXT,
    mode            VARCHAR(32)  NOT NULL DEFAULT 'synthetic'
                        CHECK (mode IN ('synthetic', 'isaac', 'gazebo', 'real')),
    status          VARCHAR(32)  NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','INITIALIZING','RUNNING','PAUSED','COMPLETED','FAILED','ABORTED')),
    world_name      VARCHAR(128) NOT NULL DEFAULT 'small_warehouse',
    robot_count     INTEGER      NOT NULL DEFAULT 1,
    duration_seconds INTEGER,
    config          JSONB        NOT NULL DEFAULT '{}',
    result_summary  JSONB,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_by      VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── faults ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS faults (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    fault_id        VARCHAR(64)  NOT NULL UNIQUE,
    robot_id        VARCHAR(64)  NOT NULL,
    simulation_run_id UUID,
    fault_type      VARCHAR(64)  NOT NULL
                        CHECK (fault_type IN (
                            'ESTOP','BATTERY_DRAIN','SENSOR_NOISE','NAVIGATION_BLOCKAGE',
                            'MOTOR_FAULT','COMM_LOSS','LOCALIZATION_DRIFT','OVERCURRENT',
                            'THERMAL_THROTTLE','CASCADING_FAILURE'
                        )),
    severity        VARCHAR(16)  NOT NULL DEFAULT 'WARNING'
                        CHECK (severity IN ('INFO','WARNING','CRITICAL','FATAL')),
    status          VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','RESOLVED','ACKNOWLEDGED','IGNORED')),
    description     TEXT,
    parameters      JSONB        NOT NULL DEFAULT '{}',
    is_cascading    BOOLEAN      NOT NULL DEFAULT FALSE,
    parent_fault_id UUID         REFERENCES faults(id),
    resolution_notes TEXT,
    injected_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    duration_seconds DOUBLE PRECISION,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── validation_runs ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS validation_runs (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          VARCHAR(64)  NOT NULL UNIQUE,
    suite_name      VARCHAR(128) NOT NULL,
    simulation_run_id UUID       REFERENCES simulation_runs(id),
    status          VARCHAR(32)  NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','RUNNING','PASSED','FAILED','ERROR','ABORTED')),
    pass_rate       DOUBLE PRECISION CHECK (pass_rate >= 0 AND pass_rate <= 1),
    total_checks    INTEGER      NOT NULL DEFAULT 0,
    passed_checks   INTEGER      NOT NULL DEFAULT 0,
    failed_checks   INTEGER      NOT NULL DEFAULT 0,
    error_checks    INTEGER      NOT NULL DEFAULT 0,
    results         JSONB        NOT NULL DEFAULT '{}',
    config          JSONB        NOT NULL DEFAULT '{}',
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    triggered_by    VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── anomalies ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS anomalies (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    anomaly_id      VARCHAR(64)  NOT NULL UNIQUE,
    robot_id        VARCHAR(64)  NOT NULL,
    simulation_run_id UUID,
    anomaly_type    VARCHAR(64)  NOT NULL,
    severity        VARCHAR(16)  NOT NULL DEFAULT 'WARNING',
    confidence      DOUBLE PRECISION CHECK (confidence >= 0 AND confidence <= 1),
    description     TEXT,
    metric_name     VARCHAR(128),
    metric_value    DOUBLE PRECISION,
    baseline_value  DOUBLE PRECISION,
    deviation_pct   DOUBLE PRECISION,
    context         JSONB        NOT NULL DEFAULT '{}',
    is_acknowledged BOOLEAN      NOT NULL DEFAULT FALSE,
    acknowledged_by VARCHAR(64),
    detected_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── users ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    username        VARCHAR(64)  NOT NULL UNIQUE,
    email           VARCHAR(256) UNIQUE,
    hashed_password VARCHAR(256) NOT NULL,
    role            VARCHAR(32)  NOT NULL DEFAULT 'OPERATOR'
                        CHECK (role IN ('ADMIN','ENGINEER','OPERATOR','VIEWER','SERVICE')),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    is_verified     BOOLEAN      NOT NULL DEFAULT FALSE,
    last_login      TIMESTAMPTZ,
    login_count     INTEGER      NOT NULL DEFAULT 0,
    preferences     JSONB        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── api_keys ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    key_hash        VARCHAR(256) NOT NULL UNIQUE,
    user_id         UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(128) NOT NULL,
    permissions     JSONB        NOT NULL DEFAULT '{}',
    last_used       TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── audit_log ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID         REFERENCES users(id),
    username        VARCHAR(64),
    action          VARCHAR(128) NOT NULL,
    resource_type   VARCHAR(64)  NOT NULL,
    resource_id     VARCHAR(64),
    details         JSONB        NOT NULL DEFAULT '{}',
    ip_address      INET,
    user_agent      TEXT,
    status          VARCHAR(16)  NOT NULL DEFAULT 'SUCCESS'
                        CHECK (status IN ('SUCCESS','FAILURE','ERROR')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── analytics_snapshots ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_type   VARCHAR(64)  NOT NULL,
    time_bucket     TIMESTAMPTZ  NOT NULL,
    granularity     VARCHAR(16)  NOT NULL DEFAULT '1h'
                        CHECK (granularity IN ('1m','5m','15m','1h','1d','1w')),
    robot_id        VARCHAR(64),
    fleet_id        VARCHAR(64),
    metrics         JSONB        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_type, time_bucket, granularity, robot_id)
);

-- ─── notifications ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    notification_id VARCHAR(64)  NOT NULL UNIQUE,
    recipient_id    UUID         REFERENCES users(id),
    channel         VARCHAR(32)  NOT NULL DEFAULT 'in_app'
                        CHECK (channel IN ('in_app','email','webhook','slack','pagerduty')),
    severity        VARCHAR(16)  NOT NULL DEFAULT 'INFO',
    title           VARCHAR(256) NOT NULL,
    message         TEXT         NOT NULL,
    payload         JSONB        NOT NULL DEFAULT '{}',
    is_read         BOOLEAN      NOT NULL DEFAULT FALSE,
    is_sent         BOOLEAN      NOT NULL DEFAULT FALSE,
    sent_at         TIMESTAMPTZ,
    read_at         TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ─── webhooks ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS webhooks (
    id              UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(128) NOT NULL,
    url             TEXT         NOT NULL,
    secret          VARCHAR(256) NOT NULL,
    events          JSONB        NOT NULL DEFAULT '["*"]',
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    failure_count   INTEGER      NOT NULL DEFAULT 0,
    last_triggered_at TIMESTAMPTZ,
    last_status_code INTEGER,
    created_by      UUID         REFERENCES users(id),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- INDEXES
-- =============================================================================

-- robots
CREATE INDEX IF NOT EXISTS idx_robots_status ON robots (status);
CREATE INDEX IF NOT EXISTS idx_robots_fleet_id ON robots (fleet_id) WHERE fleet_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_robots_last_seen ON robots (last_seen_at DESC NULLS LAST);

-- telemetry_events (non-partition table indexes)
CREATE INDEX IF NOT EXISTS idx_telemetry_robot_id ON telemetry_events (robot_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_robot_time ON telemetry_events (robot_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_mission ON telemetry_events (mission_id) WHERE mission_id IS NOT NULL;

-- faults
CREATE INDEX IF NOT EXISTS idx_faults_robot_id ON faults (robot_id);
CREATE INDEX IF NOT EXISTS idx_faults_status ON faults (status);
CREATE INDEX IF NOT EXISTS idx_faults_type ON faults (fault_type);
CREATE INDEX IF NOT EXISTS idx_faults_severity ON faults (severity);
CREATE INDEX IF NOT EXISTS idx_faults_sim_run ON faults (simulation_run_id) WHERE simulation_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_faults_injected_at ON faults (injected_at DESC);

-- simulation_runs
CREATE INDEX IF NOT EXISTS idx_sim_runs_status ON simulation_runs (status);
CREATE INDEX IF NOT EXISTS idx_sim_runs_created ON simulation_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sim_runs_created_by ON simulation_runs (created_by) WHERE created_by IS NOT NULL;

-- validation_runs
CREATE INDEX IF NOT EXISTS idx_validation_status ON validation_runs (status);
CREATE INDEX IF NOT EXISTS idx_validation_suite ON validation_runs (suite_name);
CREATE INDEX IF NOT EXISTS idx_validation_sim_run ON validation_runs (simulation_run_id) WHERE simulation_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_validation_created ON validation_runs (created_at DESC);

-- anomalies
CREATE INDEX IF NOT EXISTS idx_anomalies_robot_id ON anomalies (robot_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_type ON anomalies (anomaly_type);
CREATE INDEX IF NOT EXISTS idx_anomalies_detected_at ON anomalies (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_unacked ON anomalies (is_acknowledged) WHERE is_acknowledged = FALSE;

-- audit_log
CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_log (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log (resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at DESC);

-- analytics_snapshots
CREATE INDEX IF NOT EXISTS idx_analytics_type_bucket ON analytics_snapshots (snapshot_type, time_bucket DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_robot_bucket ON analytics_snapshots (robot_id, time_bucket DESC) WHERE robot_id IS NOT NULL;

-- notifications
CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications (recipient_id);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications (recipient_id, is_read) WHERE is_read = FALSE;
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications (created_at DESC);

-- =============================================================================
-- DEFAULT DATA
-- =============================================================================

-- Default admin user (password: Admin@arvp2024! — CHANGE IN PRODUCTION)
-- bcrypt hash of 'Admin@arvp2024!'
INSERT INTO users (
    id, username, email, hashed_password, role, is_active, is_verified, created_at
) VALUES (
    uuid_generate_v4(),
    'admin',
    'admin@arvp.local',
    '$2b$12$tc0eurHQPj5l/wnoHx9./ODfUcf74/qFfMn4cW3Ba0nUEopN8atSK',
    'ADMIN',
    TRUE,
    TRUE,
    NOW()
) ON CONFLICT (username) DO NOTHING;

-- Default service account for inter-service communication
INSERT INTO users (
    id, username, email, hashed_password, role, is_active, is_verified, created_at
) VALUES (
    uuid_generate_v4(),
    'service_account',
    'service@arvp.internal',
    '$2b$12$MLtOuXt4PS4LgdnlJGMUCuG/RahQAcibUDTme0CSNm/FjKZHqCDIy',
    'SERVICE',
    TRUE,
    TRUE,
    NOW()
) ON CONFLICT (username) DO NOTHING;

-- =============================================================================
-- TRIGGERS
-- =============================================================================

-- Auto-update updated_at on row modification
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY['robots','simulation_runs','faults','validation_runs','users','webhooks'])
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_updated_at ON %I;
             CREATE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();',
            t, t, t, t
        );
    END LOOP;
END;
$$;
