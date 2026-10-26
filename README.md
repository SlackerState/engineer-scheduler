# 工程师排班管理

把原来的「工程师排班表.xlsm」做成了桌面软件（Windows / Mac 通用），并接上 D365：自动拉取 **Udesk 创建的服务订单**，显示每单的状态、工程师和预约/出发/到达/完工时间，再把预约自动排进排班表。

## 数据从哪来

在 Udesk 里创建的服务订单（SVC-xxxxx）会被三一中间件推送到 D365 的「服务订单」实体 `mcs_serviceorder`，并带有：

| D365 字段 | 含义 |
|---|---|
| `mcs_ordersource = 60` | 订单来源 = Udesk |
| `mcs_referencenumber` | 外部系统工单号，即 Udesk 的 SVC-xxxxx |
| `mcs_ordernumber` | 服务订单号（如 2610028417） |
| `mcs_orderstatus` | 订单状态（已创建 / 已派工 / 已接收 / 已出发 / 已到达 / 现场完工 / 服务完工 …） |
| `_mcs_engineer_value` | 工程师 |
| `mcs_appointmenttime` 等 | 预约上门、派单、出发、到达、现场完工时间 |
| `_mcs_networkid_value` | 服务网点（默认只同步 `UK Kent 01 S`） |

所以软件只需要登录 D365 一个系统：按「来源 = Udesk + 最近 N 天」拉单，就能拿到 Udesk 创建的所有订单及其 D365 详情。Udesk 里建了单、但还没推送到 D365 的（CRM 工单号为空），要等推送成功后才会出现。

## 功能（对应原 Excel 各页）

- **订单清单**：自动同步 + 手工新增，可按状态、工程师、关键字筛选；点一行可编辑借料单号、处理进度等，并查看 D365 信息、直接在 D365 打开。「查 D365 订单」可以按订单号或 SVC 号临时查询。
- **排班表**：每人每天上午/下午，点格子选状态（空闲、外出、驻点、休假、培训、休息）；橙色 = 已派单，红色 = 已派单但该时段休假/培训/休息/驻点（冲突）；底部「外勤可派人数 / 驻点在岗人数」为 0 标红、为 1 标橙。
- **今日查询**：任意日期 + 时段，看谁有空、派了几单。
- **派单**：派单录入 + 实时检查（必填项、该时段状态、已派单数），提交后自动把排班格子改成「外出」，规则与原来的「提交派单」宏一致；下方是派单记录。
- **D365 预约 → 派单**：订单有工程师和预约上门时间时，自动写一条派单记录（12 点前算上午），改约后会自动挪到新时段，取消的订单会自动删掉这条派单。
- **人员**：姓名、类型（外勤/驻点）、电话、备注、**D365 别名**（D365 里叫 `Lee Ryan`、`Winzar Charles` 的人，按姓名中任一单词自动对应；对不上时在这里填别名）。
- **Excel**：导入原 xlsm（人员、排班、派单记录、订单清单，可重复导入，已有记录会跳过）；导出为 xlsx。

## 第一次使用

1. 打开软件 → 「设置」→「导入 Excel」，选原来的 `工程师排班表.xlsm`。
2. 点左下角「同步 D365」（或「设置」→「登录 D365」）。第一次会弹出 Chrome/Edge 窗口，用公司账号登录 D365（含 MFA）；登录成功后窗口自动关闭并开始同步。
3. 需要定时同步的话，在设置里勾选「自动同步」（自动同步时不会弹登录窗口，登录过期会在左下角提示）。

登录状态保存在本机的软件数据目录里（只在这台电脑上用）。平时微软账号会自动续期；彻底过期时点「同步 D365」会再弹一次登录窗口。

## 运行与打包

需要 Python 3.10 及以上，以及本机装有 Google Chrome 或 Microsoft Edge（Windows 自带 Edge）。

开发运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py          # 桌面窗口
.venv/bin/python main.py --web    # 网页模式 http://127.0.0.1:8765
```

打包（**必须在目标系统上打包**，Mac 打不出 exe）：

- Mac：`./build_mac.sh` → `dist/工程师排班管理.app`
- Windows：双击 `build_windows.bat` → `dist\工程师排班管理\工程师排班管理.exe`（整个文件夹一起复制给同事）

Windows 10/11 需要 WebView2 运行时（新系统一般自带，没有的话从微软官网安装）。

### GitHub 自动打包

仓库里的 `.github/workflows/build.yml` 会在每次推送到 `main` 时，在 GitHub 的 Windows 和 Mac 机器上各打一个包，并用网页模式启动一次做冒烟测试：

- 下载：仓库的 **Actions** 页 → 点最新一次「打包」→ 页面底部 Artifacts 里的 `EngineerScheduler-windows` / `EngineerScheduler-macos-arm64`（保留 30 天）。
- 发版：打一个 `v` 开头的标签并推送，两个包会自动发布到 **Releases**：

```bash
git tag v1.0.0
git push origin v1.0.0
```

Mac 包只支持 Apple 芯片（M 系列），而且没有苹果开发者签名：第一次打开如果提示“无法验证开发者”或“已损坏”，在终端执行 `xattr -dr com.apple.quarantine /Applications/工程师排班管理.app` 后再打开（路径换成实际位置）。Windows 首次运行可能出现 SmartScreen 提示，点「更多信息 → 仍要运行」。

## 数据存放位置

- Windows：`%APPDATA%\EngineerScheduler\`
- Mac：`~/Library/Application Support/EngineerScheduler/`

里面的 `data.db` 是全部数据（SQLite），`browser-profile/` 和 `d365-session.json` 是 D365 登录状态。换电脑时复制 `data.db` 即可，登录需要重新做一次。

## 代码结构

```
main.py               入口（桌面窗口 / --web 网页模式）
backend/db.py         SQLite 表结构、设置
backend/logic.py      排班/派单规则（对应原 Excel 公式和宏）
backend/browser.py    Playwright 驱动 Chrome/Edge，保持 D365 登录、调 Web API
backend/d365.py       mcs_serviceorder 查询与字段映射
backend/sync.py       同步流程（拉单 → 订单清单 → 派单/排班）
backend/excel_io.py   Excel 导入导出
backend/api.py        界面调用的接口
ui/                   界面（HTML/CSS/JS，无外部依赖）
```

在设置里可以调整：D365 网址、订单来源、服务网点、回看天数、是否用 D365 状态覆盖本地状态、是否把预约写入派单、上午/下午分界、用哪个浏览器、同步时是否在后台运行浏览器。
