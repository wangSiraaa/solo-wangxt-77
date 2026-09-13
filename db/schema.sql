-- SplitGuard PostgreSQL reference schema (SQLAlchemy models mirror this).
CREATE TABLE datasets (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(200) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 来源对象（主体）：身份锚点，原始与派生样本共享同一来源身份
CREATE TABLE sources (
    id           SERIAL PRIMARY KEY,
    dataset_id   INT NOT NULL REFERENCES datasets(id),
    external_key VARCHAR(200) NOT NULL,
    UNIQUE (dataset_id, external_key)
);

CREATE TABLE samples (
    id           SERIAL PRIMARY KEY,
    dataset_id   INT NOT NULL REFERENCES datasets(id),
    sample_key   VARCHAR(200) NOT NULL,
    label        VARCHAR(100) NOT NULL,
    captured_at  TIMESTAMPTZ NOT NULL,
    kind         VARCHAR(20) NOT NULL DEFAULT 'raw',   -- raw | crop | augment | transcode
    source_id    INT REFERENCES sources(id),           -- 派生样本允许缺失
    content_hash VARCHAR(64),                          -- 重复内容仅作辅助证据
    UNIQUE (dataset_id, sample_key)
);
CREATE INDEX idx_samples_source ON samples(source_id);
CREATE INDEX idx_samples_content_hash ON samples(content_hash) WHERE content_hash IS NOT NULL;

-- 派生关系：子样本沿边继承来源身份
CREATE TABLE source_relations (
    id               SERIAL PRIMARY KEY,
    dataset_id       INT NOT NULL REFERENCES datasets(id),
    parent_sample_id INT NOT NULL REFERENCES samples(id),
    child_sample_id  INT NOT NULL REFERENCES samples(id),
    relation_type    VARCHAR(20) NOT NULL,             -- crop | augment | transcode
    UNIQUE (parent_sample_id, child_sample_id)
);

-- 拆分版本：确认后锁定 manifest_hash；任何修改生成新版本行
CREATE TABLE split_versions (
    id                SERIAL PRIMARY KEY,
    dataset_id        INT NOT NULL REFERENCES datasets(id),
    version_no        INT NOT NULL,
    parent_version_id INT REFERENCES split_versions(id),
    status            VARCHAR(20) NOT NULL DEFAULT 'draft',  -- draft | locked | superseded
    seed              INT NOT NULL,
    params            JSONB NOT NULL,                   -- 目标比例 / 时间边界 / 权重
    manifest_hash     VARCHAR(64),
    report            JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at         TIMESTAMPTZ,
    UNIQUE (dataset_id, version_no)
);

CREATE TABLE assignments (
    id               SERIAL PRIMARY KEY,
    split_version_id INT NOT NULL REFERENCES split_versions(id),
    sample_id        INT NOT NULL REFERENCES samples(id),
    side             VARCHAR(10) NOT NULL CHECK (side IN ('train', 'eval')),
    UNIQUE (split_version_id, sample_id)
);
CREATE INDEX idx_assignments_version ON assignments(split_version_id);

CREATE TABLE audit_events (
    id               SERIAL PRIMARY KEY,
    split_version_id INT NOT NULL REFERENCES split_versions(id),
    event            VARCHAR(50) NOT NULL,             -- generated | verified | locked | revised
    detail           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
