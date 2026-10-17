-- 控制面数据库初始化（仅在数据卷为空、容器首次启动时执行一次）
-- 表结构由 Alembic 管理，本文件只做「扩展」与「只读审计角色」这类 DBA 级准备。

-- 模糊检索（数据源名、Pipeline 名、错误码搜索）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 审计角色：审计人员只读访问运行、质量与证据数据（对应 PRD §2.1 审计人员）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'elt_auditor') THEN
        CREATE ROLE elt_auditor WITH LOGIN PASSWORD 'CHANGE_ME_AUDITOR';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE elt_agent TO elt_auditor;
GRANT USAGE ON SCHEMA public TO elt_auditor;
-- 未来由 Alembic 新建的表自动授予只读（默认权限只影响之后创建的对象）
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO elt_auditor;

-- 证据账本与运行历史为审计对象，禁止审计角色写入（只读即天然防篡改的一层）
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM elt_auditor;
