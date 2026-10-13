-- 仅供 DBA 在用户确认凭证创建后人工执行，不挂入 MySQL 自动初始化目录。
-- 先用安全交互方式分别创建 temporal_runtime 与 temporal_migration，禁止复用口令。
-- 两个账号均须是无全局权限、无角色继承的新专用身份；已有账号先人工审计 SHOW GRANTS。
-- 此处不创建账号、不嵌入密码、不轮换现有 AIOps 或 root 凭证。
CREATE DATABASE IF NOT EXISTS temporal_prod CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
CREATE DATABASE IF NOT EXISTS temporal_visibility_prod CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
GRANT SELECT, INSERT, UPDATE, DELETE ON temporal_prod.* TO 'temporal_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON temporal_visibility_prod.* TO 'temporal_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON temporal_prod.* TO 'temporal_migration'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON temporal_visibility_prod.* TO 'temporal_migration'@'%';
-- '%' 仅适用于可信 Compose 私网及宿主 loopback；不代表多租户或互联网安全边界。
-- 迁移完成后由 DBA 锁定迁移账号或回收授权；Server 不持有该账号的密码。
