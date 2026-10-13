#!/bin/sh
# 独立维护作业；固定目标与身份，绝不由 Server 每次启动自动调用。
set -eu
umask 077

fail() { printf '%s\n' "$1" >&2; exit 1; }

# 工具报错可能带连接信息；不把原始 stderr 写入 Compose 日志。
run_sql() {
    temporal-sql-tool --plugin mysql8 --ep mysql -p 3306 \
        -u temporal_migration --db "$1" "$2" "$3" "$4" \
        >/tmp/temporal-schema.log 2>&1 || fail "schema 操作失败，停止后续步骤；请在受控环境排查，禁止回退 root"
}

case "${1:-}" in
  schema)
    [ "${AIOPS_SCHEMA_APPROVED:-NO}" = YES ] || fail '尚未单独确认 schema 维护操作'
    [ -s /run/secrets/temporal_migration_password ] || fail '缺少迁移专用凭证'
    SQL_PASSWORD=$(cat /run/secrets/temporal_migration_password)
    [ -n "$SQL_PASSWORD" ] || fail '迁移专用凭证为空'
    export SQL_PASSWORD
    # 官方工具负责版本表幂等检查；不使用 --overwrite，不忽略错误，不删除数据库。
    run_sql temporal_prod setup-schema -v 0.0
    run_sql temporal_prod update-schema -d /etc/temporal/schema/mysql/v8/temporal/versioned
    run_sql temporal_visibility_prod setup-schema -v 0.0
    run_sql temporal_visibility_prod update-schema -d /etc/temporal/schema/mysql/v8/visibility/versioned
    unset SQL_PASSWORD
    printf '%s\n' '两库 schema 操作成功；仍须校验 runtime 权限和重启持久化'
    ;;
  namespace-create|namespace-update)
    [ "${AIOPS_NAMESPACE_APPROVED:-NO}" = YES ] || fail '尚未单独确认 namespace 操作'
    # 创建失败不擅自更新；已存在时先核对，再显式选择 namespace-update。
    action=${1#namespace-}
    temporal --address temporal-prod:7233 operator namespace "$action" \
        --namespace aiops-production --retention 720h \
        >/tmp/temporal-namespace.log 2>&1 || fail 'namespace 操作失败，未回退 demo/default'
    temporal --address temporal-prod:7233 operator namespace describe \
        --namespace aiops-production || fail 'namespace 操作后核验失败'
    ;;
  *) fail '仅支持 schema、namespace-create 或 namespace-update' ;;
esac
