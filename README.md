# luci-app-e5-esim

E5 设备的 LuCI eSIM 管理应用。菜单入口为“蜂窝 → eSIM 管理”，使用 apk 软件源的 lpac，沿用信息屏 eSIM 应用的 AT 桥接和操作流程。

## 功能

- SIM 1 / SIM 2 选择；首次打开时先读取 SIM 1，读取失败则尝试 SIM 2。
- 多 SE：读取到芯片信息后探测 estk 厂商应用（`A06573746B6D65FFFFFFFFFFFF6D6774`），
  依次识别 SE 0、SE 1；两个都可用时显示选择器，所选 SE 的 ISD-R AID
  贯穿本页全部 lpac 操作，切换 SE 即重新读取该 SE 的芯片、配置文件与通知。
- 芯片 EID、固件、SGP.22、配置格式和 GlobalPlatform 版本、存储空间、证书公钥标识与 SAS 认证信息。
- 配置文件启用、停用、重命名和删除；删除入口仅在未启用的配置文件上显示。
  启停后基带重读 SIM，并每 3 秒读取一次配置列表直到目标状态生效（最多 6 次）；
  仍未生效时提示稍后重试。
- 激活码下载、可选确认码、运营商元数据预览、确认与取消。
- 通知单条处理、单条删除、处理全部与全部删除。批量操作按序号升序执行，单条失败后继续。
- 下载或删除配置文件后，仅上报本次操作新增的通知；上报失败会提示在通知页重试。
- 打开页面时检查 `lpac` 与 `libcurl-gnutls4` 运行依赖；缺失时显示实际路径、安装命令和软件包管理入口，安装后可重新检测。

启停、重命名、删除、批量通知和下载均由后台任务执行。刷新或关闭页面后，已经开始的操作继续执行；重新进入页面会接续显示正在运行的任务。

## 安装

依赖为 `luci-base`、`rpcd`、`python3`、`lpac`、`libcurl-gnutls4`，安装本包时由包管理器一并装入。
页面会检查两个外部运行组件：`/usr/lib/lpac` 必须存在且可执行，`/usr/lib/libcurl-gnutls.so.4` 必须存在且可读。
缺失时页面列出缺少的包和实际路径；可按页面给出的命令安装，或打开 LuCI 软件包管理（`/admin/system/opkg`）安装：

```sh
apk update && apk add lpac libcurl-gnutls4
```

安装后回到页面点击“重新检测”。`luci-base`、`rpcd`、`python3` 由基础系统或本包依赖提供；
`python3` 是 rpcd 脚本的运行环境，缺失时页面无法启动，因此不在页面内检测。

将本目录放入 OpenWrt 构建树的 `package/luci-app-e5-esim`：

```sh
make package/luci-app-e5-esim/compile
```

也可在设备上从本目录直接部署；直接复制不会安装依赖，需先确保上述两个运行组件存在：

```sh
cp -r root/. /
cp -r htdocs/. /www/
chmod 0755 /usr/libexec/rpcd/e5-esim
rm -f /tmp/luci-indexcache
rm -rf /tmp/luci-modulecache
/etc/init.d/rpcd restart
```

默认 AT 环为 `/dev/stty_nr4`。`E5_ESIM_AT_DEV` 可在 rpcd 的运行环境中覆盖该路径。

## 操作流程

启用或停用先执行 `profile enable|disable`，成功后按 `+SFUN=5`、等待 1 秒、`+SFUN=4`、等待 1 秒、`+SFUN=2` 的顺序刷新目标 SIM。之后每 3 秒读取一次配置列表，直到目标 profile 的状态生效（最多 6 次）；仍未生效时提示稍后重试。刷新命令失败会单独提示。

下载和删除先记录通知列表，操作成功后重新读取通知，按序号差集上报新增通知。网络上报失败不改变配置文件操作的成功结果。

删除遇到 lpac 2.3.0 的 `es10c_delete_profile` / `internal error, maybe illegal iccid/aid coding` 时，读取一次配置列表核对结果。目标已消失则按已删除继续处理新增通知，原始错误保留在任务的 `diagnostic` 字段；目标仍存在或读取失败时保留原错误。整个流程只发送一次删除命令。

页面使用 LuCI 原生的分区、表格、表单、卡槽标签、按钮、弹窗和进度条样式，随当前主题显示。

下载使用 `profile download -p -a <激活码> [-c <确认码>]`。lpac 输出 `preview` 后暂停等待确认，页面显示运营商、名称、ICCID 和类别。后台记录与任务 PID 对应的取消请求，下载前读取通知、预览、下载和自动通知上报均支持取消。HTTP 等待阶段给 lpac 约 3 秒收尾时间；正在执行的 AT 命令先完成或按自身超时结束，再执行退出清理。取消结果记录为 `cancelled`；已经报告安装成功的任务仍按成功处理，上报未完成的通知可稍后重试。

下载启动响应丢失时，页面先查询并接管已运行的任务。取消请求失败或连续三次无法查询状态时，页面提供状态查询和取消重试入口；任务结果确认前保留激活码和目标卡槽。

写入阶段从 BoundProfilePackage 的 BF36 BER 长度取得总字节数，只累计卡片返回成功状态的 STORE DATA 负载。GET RESPONSE 和失败重试不重复计数；任务的 `write` 字段提供 `written`、`total`、`percent`，页面显示写入百分比和已发送字节数。达到 100% 后显示确认安装结果，任务成功仍以 lpac 最终结果为准。服务器认证与网络获取阶段显示当前步骤。

## 后端

| 文件 | 职责 |
|---|---|
| `root/usr/lib/e5-esim/bridge.py` | pty AT 桥、lpac 运行器、SIM 刷新 |
| `root/usr/lib/e5-esim/job.py` | 后台操作、通知上报、下载确认 |
| `root/usr/libexec/rpcd/e5-esim` | 参数校验、依赖检测、互斥、ubus RPC |
| `htdocs/luci-static/resources/view/e5-esim.js` | LuCI 页面与任务显示 |

ubus 对象为 `e5-esim`。读取方法为 `dependencies`、`snapshot`、`profiles`、`notifications` 和 `job_status`。
`dependencies` 无参数，返回 `ready`、`missing`（缺失包名）以及 `lpac`、`libcurl-gnutls4` 的实际路径和存在状态；
它只检查文件，不访问 AT 环，也不使用操作锁。页面通过 `operation_start`、`download_start` 启动任务，通过 `job_confirm`、`job_cancel` 控制下载。`job_cancel` 只对运行中的下载写入 PID 取消请求；没有活动下载或目标是操作任务时返回失败。底层 profile、notification 与 SIM 刷新方法也可通过 ubus 单独调用。

`snapshot` 参数为 `{slot, aid, scan}`；`aid` 为空或 SE 白名单 AID（`…442D522030` / `…442D522031`），
`scan` 控制是否探测多 SE。探测结果以 `ses` 位掩码返回，`aid` 是最终选中的 ISD-R；
已扫描过再读时用 `scan:false` 保留已有掩码。其余 lpac 操作方法接受同一个可选 `aid`，
后台任务把目标 `aid` 固化在任务状态里，页面刷新重连后继续操作同一个 SE。

所有访问 lpac 和 AT 环的 RPC 使用 `/tmp/e5-esim/lock` 的非阻塞独占锁。后台任务继承锁文件描述符并持有到整个操作结束。任务先发布终态，再显式关闭继承的锁描述符，最后按 PID 条件删除 PID 文件；因此 `running` 只有在锁释放后才可能变为 false。父进程先写入 PID 和运行状态，再通过管道放行子进程，避免启动状态与任务输出相互覆盖。任务状态采用临时文件加 rename 原子更新。

`job_status` 返回任务类型、卡槽、操作、阶段、预览元数据、结果和存活状态。状态文件、PID、取消请求与确认 FIFO 位于 `/tmp/e5-esim/`，设备重启后清空。任务参数与卡片身份信息仅应向获授权的 LuCI 用户开放。

## AT 桥

lpac 使用 `at` APDU 后端，设备路径指向 pty。桥接将 `CCHO`、`CGLA`、`CCHC` 转发为带 `AT+SPACTCARD=<slot>;` 前缀的命令，直接访问空闲 AT 环。

- `+CME ERROR` 后补整行 `ERROR`，使 lpac 的 AT 驱动结束等待。
- CCHO 失败后依次关闭 1～3 号残留逻辑通道，随后重试一次。
- CME 10、21、100、3 触发 `+SFUN=2` 恢复，再按 1 秒间隔最多探测五次 CCID。SFUN 命令独立发送。
- CGLA 遇到掉卡错误时恢复、重新开通道并重发一次。实际通道号由桥接记录。
- 普通 lpac 命令看门狗为 120 秒，下载为 300 秒；下载预览等待期间暂停计时。

运行器通过 `/tmp/e5-lpac-tls/libcurl.so.4` 加载 GnuTLS 版 libcurl，以处理部分 GSMA 测试服务器的 TLS 证书扩展。选定 SE 时给 lpac 设置 `LPAC_CUSTOM_ISD_R_AID`；标准 ISD-R 的调用不携带该变量。设备的 lpac 2.3.0 使用 `es8p_meatadata_parse` 进度名，页面同时识别新版的正确拼写。

## 使用与验证

`/dev/stty_nr4` 与 ModemManager 使用的 `/dev/wwan0at0` 分离。插件的互斥锁只覆盖本插件；操作期间应避免同时运行信息屏 eSIM 管理、原版桥接脚本或其他 SIM 切换工具。

配置文件的 eUICC 状态和基带读取的 ICCID 是两个观测值。AT 刷新返回 OK 仅表示命令执行成功，不能据此断言基带身份已经更新。遇到状态不一致时应保留两侧信息与 APDU 应答，结合设备固件和 lpac 同版本源码诊断。

本项目的回归测试使用模拟 AT、lpac 子进程和 LuCI RPC，覆盖运行依赖检测、通道恢复与重试、忽略 SIGINT 的取消、AT 交换保护、预览与初始通知读取取消、互斥、参数校验、切换刷新顺序、通知差集和部分失败继续、卡槽选择、取消终态及锁释放：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node tests/frontend.cjs
```

实机此前已跑通读取、重命名、通知处理/删除。启停测试中出现过 eUICC 状态与基带 ICCID 不一致，以及 APDU 返回 `6200` 的现象；这些记录不能作为启停完整验收通过的依据。完整下载预览与写入仍需使用有效激活码完成端到端验收。
