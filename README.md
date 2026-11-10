# GitHub 自动备份

部署目录：`/opt/github-backup`。Docker Compose 运行两个服务：`scheduler` 定时备份，`bot` 提供 Telegram 命令和通知。配置、凭据和工作数据全部在本目录下；不会写入镜像。

## 使用

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 50 scheduler
docker compose exec scheduler python -m ghbackup status
docker compose down                    # 停止服务，不删除数据
```

唯一需要手动配置的文件是根目录 `config.toml`。它包含 GitHub Token、Google OAuth 客户端与刷新令牌、Telegram Token/User ID，以及备份选项。文件权限为 `0600`，已被 Git 和 Docker 构建排除。

新部署先复制 `config.example.toml` 为 `config.toml` 并填写。Docker 使用固定 UID/GID `10001`，数据目录需由该用户拥有：

```bash
cp config.example.toml config.toml
mkdir -p data restore
chown -R 10001:10001 config.toml data restore
chmod 600 config.toml
docker compose build
python3 scripts/setup-storage.py        # root：配置有硬上限的工作盘及启动顺序
```

`storage.img` 是最多 8,000,000,000 字节的稀疏 ext4 工作盘，按实际使用占空间，包含全部运行数据和恢复输出；文件系统从内核限制容量。每 5 分钟回收空闲块，`du -h storage.img` 可查看实际占用，`ls -lh` 显示的是容量上限。`data/`、`restore/` 是指向该盘的兼容入口。工作盘未挂载时容器拒绝写入，不会回退到 VPS 主盘。Docker 镜像、构建缓存及轮转日志属于额外系统开销。

成功备份后自动删除本地 Git 和元数据缓存，只留仓库清单、Git refs、凭据缓存和任务状态。源 Git refs 未变化时不重新下载；变化时重新拉取 Git，元数据按需从 Drive 恢复再增量导出，修复目标仓库时也可从 Drive 取回 Git。因此更省本地空间，但下载流量和同步耗时会增加。

元数据同步在仓库之间交还文件锁，让等待的 Git 任务先运行；重新获得锁后重载状态，避免覆盖另一任务的进度。

每次备份/恢复前及每小时自动清理中断遗留的临时目录。恢复导出默认保留 24 小时，可通过 `restore_retention_hours` 修改；有用的恢复文件应及时移出。未完成采集、未补传版本不因清理而删除。Drive 已保存完整 Git 恢复点时，GitHub 故障只保留待发布状态，恢复后从 Drive 补齐，避免目标故障堆积本地缓存。剩余不足 `minimum_free_bytes`（默认 1 GiB）时停止新采集并告警；内核硬上限同时约束单次大文件写入。Drive 历史不设容量或保留期上限。

`data/runtime/rclone.conf` 是程序维护的 OAuth 缓存，不需要单独配置或提交。真实配置只读挂载进容器，不会写入镜像或备份恢复模板。手动执行清理可用 `docker compose exec scheduler python -m ghbackup --wait-lock cleanup`。

源账号只读；朋友账号原有仓库的 ID 记录在 `protected_destination_ids`，禁止修改。新备份仓库独立创建为 Private。按源仓库 ID 跟踪更名；源删除或不可访问时保留已有备份。GitHub 限流时暂停目标写入，Drive 备份继续。

两条输出链路独立：目标 GitHub 故障不会阻断 Drive 采集；Drive 故障时，采集版本先进入本地持久队列，GitHub 可继续更新，Drive 恢复后按顺序补传。未上传队列占用 VPS 空间；磁盘不足时停止新采集并保留队列。Drive 离线期间，未补传的历史仍依赖 VPS，请关注告警。

Drive 已确认离线时，本轮不再重复上传。补传任务按仓库隔离：坏任务保留并报警，只阻止同仓库后续版本，其他仓库继续补传。元数据缓存淘汰后，必须先取回已有历史；无法取回时暂停该仓库的元数据采集，不将缺少历史的全量抓取标成完整归档。元数据导出保留可恢复基线，失败采集先保存为不完整数据，再恢复基线重试；更名通过日志同步更新两层校验清单。

Git 副本修复优先从 Drive 取回；Drive 不可用时可重新从源获取，但必须与已捕获的 refs、LFS 和 Wiki 清单一致，版本不匹配则拒绝修复。恢复帮助统一使用 `/restore/`，恢复输出仍按保留期限自动清理。

增量元数据采集前校验已有 SHA256，损坏时中止并报警，不重新信任损坏内容；应先从有效恢复点恢复或重建该资源。恢复点索引位于 `_系统/index/仓库ID/`，恢复时只读取该仓库的索引；旧索引复制迁移后保留原文件。仅同时拥有完整 Git 和元数据恢复点的仓库参与完整恢复校验。

`attest_uninitialized_wikis = true` 表示用户确认默认所有仓库未使用 Wiki，新仓库自动适用；可读取的 Wiki 仍照常备份，曾有 Wiki 历史的仓库读取失败仍报警。未作此确认时保持 `false`，或用 `empty_wiki_ids` 逐仓库确认。

## Drive 存储结构

```text
GitHub-Backup/
├── README.md
├── 仓库名--仓库ID/
│   ├── 代码/              默认分支的普通文件，可直接查看或下载
│   ├── 元数据/            Issues、PR、评论、Discussions、Releases 和附件
│   ├── Git历史/           全部分支、Tags、提交、PR refs、LFS、Wiki
│   ├── 历史/日期/时间/    被替换或删除文件的旧版本
│   └── 恢复点/日期/时间/  文件校验清单与恢复所需状态
└── _系统/                 仓库映射、恢复点索引和恢复程序
```

**新备份不加密、不打包源码。** 访问权限由 Google Drive 管理；网页是否提供预览取决于文件类型，文件本身保持原始内容。符号链接和子模块在“代码”中显示为说明文件，其原始 Git 信息保留在“Git历史”中。

第一次上传完整数据，包括可读代码和 Git 历史。之后 rclone 按内容校验，只传新增或变化的文件；旧文件先移入“历史”，再更新当前文件。每个恢复点写完整清单，不复制整份仓库。Git pack 文件如果变化，可能需要上传较大的新 pack；这不是逐行上传。

代码和分支每 5 分钟检查，元数据每 15 分钟同步，每日全量核对。每周抽查一个仓库的恢复点，每月做一次恢复演练。任务共用文件锁，避免同时改动备份。历史不自动清理。首次上传很多小文件时会较慢。

## 恢复

```bash
docker compose exec scheduler python -m ghbackup snapshots
docker compose exec scheduler python -m ghbackup restore --repo-id 仓库ID --target /restore/新目录
# 指定旧版本时增加 --snapshot 恢复点ID
```

恢复会结合当前文件、旧文件和校验清单重建所选版本，然后验证 Git/LFS 和元数据。导出目录在宿主机 `restore/` 中。明文备份不需要解密密码，也不需要源 GitHub 账号；新机器重新授权 Drive 后即可恢复。

在仅用于恢复的新机器上，填写 Drive 凭据并构建镜像，执行 `python3 scripts/setup-storage.py --no-start` 准备工作盘，再用 `docker compose run --rm scheduler restore ...` 恢复；不需要 GitHub Token，也不启动后台备份。

只有完整恢复点作为默认恢复来源；失败采集单独保留。只能恢复已经捕获的版本，不能补回安装前已丢失或两次采集之间立即删除的数据。GitHub 不允许完整重建原作者、时间和历史 PR 合并状态，因此相关数据保存为原始 JSON/附件。Actions Secrets 原值、Packages 和外部依赖不在采集范围。

## Telegram

仅接受配置中数字 User ID 的私聊；默认隐藏仓库名，不发送代码、Token、密码或原始日志。

`/status` 状态 · `/dashboard` 中文图表 · `/repos` 分页 · `/errors` 诊断 · `/backup` 增量备份 · `/verify` 校验 · `/drill` 恢复演练 · `/history` 任务记录 · `/restore` 恢复说明。

任务和通知落盘、重复提交去重；失败提醒、恢复通知、每日摘要由独立 Bot 服务发送。待发送事件使用独立索引；已发送事件保留 30 天，未发送事件不自动清理。整个 VPS 宕机时，本机 Bot 无法发消息，需要外部监测。

## 关键验证

测试覆盖仓库身份与原有仓库保护、两条备份链路故障隔离和补传、明文增量和旧版本恢复、元数据损坏、写入冷却、索引迁移、Wiki 一致性，以及 Bot 权限与任务队列。全部测试使用临时本地目录，不访问真实账号。

CI 自动执行 Docker 构建和隔离测试。Python 运行依赖、传递依赖及构建工具固定版本和 SHA256，安装强制校验哈希；基础镜像及工具二进制固定摘要。操作系统 apt 安全包仍随仓库更新，不承诺整镜像逐字节复现。
