# 镜稿共享 Skill

在 Codex 中创作电商视频完整脚本，统一支持三种功能：

| 功能 | 创作依据 | 交付 |
| --- | --- | --- |
| 产品原创 | 商品图片与已确认资料 | 原创口播、分镜、完整视频提示词 |
| 参考迁移 | 参考视频的结构、节奏和镜头 | 适配当前商品的独立脚本 |
| 文案转分镜 | 用户指定或锁定的文案 | 对应画面、运镜、音频与分镜 |

每次创作先读取站点当前发布的规则，完成后保存完整 JSON 和 Markdown，并通过成员身份上传归档。摘要、商品和标签用于整理，不替代完整脚本。

## 安装到 Codex

需要 **Python 3.10 或更新版本**，仅使用 Python 标准库。仓库的安装子目录是 `ecommerce-video-script-shared`，不要把整个仓库复制成 Skill。

把下面这句话发给 Codex：

```text
请使用 skill-installer 安装这个 GitHub Skill：
https://github.com/lekker1233/ecommerce-video-script-shared/tree/main/ecommerce-video-script-shared
```

Codex 的安装器也支持以下命令。将 `<skill-installer目录>` 替换为当前环境中该系统技能的实际位置：

```text
python "<skill-installer目录>/scripts/install-skill-from-github.py" --url https://github.com/lekker1233/ecommerce-video-script-shared/tree/main/ecommerce-video-script-shared
```

默认安装到 `$CODEX_HOME/skills/ecommerce-video-script-shared`，未设置 `CODEX_HOME` 时为 `~/.codex/skills/ecommerce-video-script-shared`。安装完成后在下一轮对话中使用；已有同名 Skill 时，先核对安装来源，再使用下方更新入口，避免重复安装覆盖。

## 首次加入网站

向站点管理员获取个人邀请链接或邀请码。默认站点为 [镜稿后台](https://scripts.wenran007.com)，成员通过客户端管理自己的脚本，管理员网页账号不会随 Skill 分发。

**接入前请确认：完整脚本、商品信息和标签会上传到你选择的站点，该站点管理员可以查看和整理；成员只能读取自己的脚本。本地图片、视频等素材文件不会自动上传。**

在 Skill 安装目录之外运行下列命令，将 `<Skill安装目录>` 替换为实际绝对路径。Windows 上，终端留在安装目录内可能阻止更新器替换该目录：

```text
python "<Skill安装目录>/scripts/shared_client.py" setup --server https://scripts.wenran007.com
```

命令会说明上传范围，要求输入 `YES`，再隐藏输入邀请码。不要把管理员密码或管理员 API 令牌填入这里；每位成员使用独立邀请兑换自己的凭据。

网络中断时用原邀请码重试，可恢复同一次兑换。邀请码输错、过期或撤销后，尚未接入的用户可以明确换用新邀请：

```text
python "<Skill安装目录>/scripts/shared_client.py" setup --server https://scripts.wenran007.com --reset-pending
```

这个选项不会替换已经连接的成员身份。已连接成员需要更换站点或身份时，应先保留本地稿件并与管理员协调。

## 日常创作和更新

调用 `ecommerce-video-script-shared` 后，Codex 按入口说明执行两类检查：

1. **每天首次使用时检查安装更新。** 从本仓库 `main/ecommerce-video-script-shared/distribution.json` 获取版本清单，再从清单指定的 `v<版本>` 标签下载文件，逐项验证 SHA-256 后更新。没有常驻进程，也不创建后台定时任务；当天不使用就不会自动检查。
2. **每份新稿开始前读取网站当前规则。** `refresh` 获取站点已发布的七份创作规则，记录版本和哈希。进行中的任务固定使用开始时的版本，发布新规则不会改写正在创作的稿件。

安装更新命令同样从 Skill 安装目录之外执行：

```text
python "<Skill安装目录>/scripts/update_skill.py"
python "<Skill安装目录>/scripts/update_skill.py" --force
```

`--force` 用于当日再次检查最新安装版本。更新只处理 `distribution.json` 白名单中的 Skill 文件，并校验固定仓库、版本标签和文件哈希；网站规则接口只发布 Markdown 内容。

此前下载的旧安装包若没有 `scripts/update_skill.py`，需要先安装一次带更新器的新版，之后才能使用每日检查。旧包不会凭空获得新增程序；迁移时保留安装目录之外的成员配置和本地作品库。

若安装的是 `2026.10.20.2`，并在 Windows 上遇到 `whoami` 编码报错，该旧更新器无法启动。请从 Skill 目录外把旧安装备份到 Skill 扫描目录外，再用安装器安装当前版；保留 `$CODEX_HOME/ecommerce-video-script-shared` 中的连接与稿件。`2026.10.20.3` 已修正 SID 的字节读取方式。

规则刷新及完整稿件保存：

```text
python "<Skill安装目录>/scripts/shared_client.py" refresh
python "<Skill安装目录>/scripts/shared_client.py" save --input <完整任务JSON路径> --release-file <refresh返回的manifest.json路径>
```

保存后保留返回的脚本编号。修订时使用同一编号并指定父版本：

```text
python "<Skill安装目录>/scripts/shared_client.py" save --input <修订JSON路径> --release-file <本次manifest.json路径> --script-id <UUID> --parent-version <当前版本>
python "<Skill安装目录>/scripts/shared_client.py" show --script-id <UUID>
python "<Skill安装目录>/scripts/shared_client.py" sync --script-id <UUID>
```

`local_saved=true` 表示本地已保存；只有 `remote_sync.status=synced` 才表示站点已接收并通过完整内容回读验证。上传失败保留本地稿件和待传状态，之后用 `sync` 补传。成员停用、认证失败或版本冲突时，应先联系管理员处理。

## 配置与稿件保护

成员配置、令牌、规则缓存和本地稿件独立保存在 `$CODEX_HOME/ecommerce-video-script-shared`，默认是 `~/.codex/ecommerce-video-script-shared`。它与 Skill 安装目录分开，安装更新不会删除或覆盖这些资料，也不会更换成员身份、上传站点或历史版本。

仓库内的 `release.json` 是发布时的七份规则快照，方便阅读三类创作流程和输出格式。实际新任务仍以网站 `refresh` 成功返回的版本为准；无法确认最新规则时，客户端会报告错误，不把旧快照冒充当前版本。运行时更新检查失败时也应说明实际状态，不宣称已经更新。

## 发布维护

维护者在自己的工作区审核共享入口、客户端和七份规则，然后通过发布工具生成安装包与独立仓库目录。下列参数为通用占位符，不包含任何管理员私有目录或凭据：

```text
python shared-skill/tools/build_release.py --source-skill <管理员创作Skill源目录> --version <新规则版本> --notes <更新说明> --output <规则JSON输出路径> --package <安装ZIP输出路径> --distribution-version <数字点分安装版本>
python shared-skill/tools/prepare_github.py --source-skill <已审核的共享Skill目录> --release <规则JSON输出路径> --version <同一安装版本> --output <独立发布仓库目录>
```

这些构建工具属于维护工作区，不是成员需要执行的命令。公共仓库只包含安装文件、版本清单、本说明和 Git 字节保留设置，不复制后台源码、测试数据、成员配置、数据库或访问凭据。

安装版本使用 2–4 段无前导零数字，例如 `2026.10.20.2`，总长度不超过 64 字符。检查独立目录的文件清单和哈希后，为同一次提交创建并推送相应的 `v<版本>` 标签，再更新 `main`。版本清单中的 `ref` 必须与已存在标签一致；先发布标签，再更新 `main` 中用于发现的清单，避免成员检查到尚未可下载的版本。已发布标签保持不变，后续修改使用新版本。网站规则还需按后台的发布流程发布并回读核验；GitHub 安装版本和网站创作规则版本各自记录，不相互替代。

本仓库未附加 MIT、Apache 等开放源代码许可证，也未据此自动授予再分发或其他许可；需要额外授权时请联系仓库维护者。
