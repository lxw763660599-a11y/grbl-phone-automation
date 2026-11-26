# BtWriter Phone

用一台 GRBL 写字机的笔尖去点手机屏幕，摄像头做闭环确认，本机 HTTP / MCP 接口给 AI 调用。

不装 App、不开 ADB、不需要 root，也不受 iOS 限制——物理触控，系统界面、任何 App 都能点。

> Windows 专用（tkinter 控制台、CH340 串口、DirectShow/MSMF 摄像头）。

## 为什么这样做

- **跨系统**：Android / iOS / 鸿蒙都行，不依赖调试授权和自动化框架。
- **不会被判定为模拟输入**：对手机来说就是一次真实触摸。
- **可以点任何东西**：系统弹窗、通知栏、支付确认、第三方 App。
- **代价是慢**：一次点按 1～3 秒（取决于机头行程），适合低频、需要判断的自动化，不适合刷量。

## 工作原理

```
        ┌──────────────┐   画面    ┌──────────────┐
        │  摄像头       │─────────▶│  AI / 脚本    │
        │ (iVCam 等)   │◀─────────│  规划下一步    │
        └──────┬───────┘   动作    └──────┬───────┘
               │                          │ u/v 归一化坐标
               │                          ▼
               │                  ┌──────────────┐
               │   park 让位      │ grbl_service │  127.0.0.1:8768
               ▼                  │  串口独占     │
        ┌──────────────┐          └──────┬───────┘
        │  写字机笔尖   │◀────────────────┘ G1 X/Y + Z 下压
        │  (GRBL/CH340)│
        └──────────────┘
```

1. 机器原点 = 手机屏幕中心，所有目标坐标用 `u/v ∈ [0,1]` 表示。
2. 标定得到的仿射矩阵把屏幕坐标换算成机器 XY，因此手机可以斜放。
3. Z 只发下压动作，抬笔靠笔架弹簧回弹，再用 `G92` 重设逻辑 Z 坐标（不发 Z 上行，避免打齿）。
4. 动作完成后机头移到摄像头不遮挡的位置（park），服务返回后才取新帧。**API 返回 ok 不等于点中了**，必须看新画面。

## 硬件与前置

- GRBL 写字机（本方案在 BtWriter / CH340 上实测），行程 ≥ 300×300 mm，Z 行程约 9 mm。
- 手机固定在写字机工作台上，屏幕中心大致对准机器行程中心。
- 一个能俯拍屏幕的摄像头：iVCam、DroidCam、USB 摄像头均可，索引写进 `tapper_config.json`。
- Python 3.10+。

## 快速开始

```powershell
git clone https://github.com/lxw763660599-a11y/btwriter-phone.git
cd btwriter-phone
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

1. **摆放设备**：笔尖能覆盖整个屏幕，机头停靠位不挡摄像头视线；不要让其他程序占用串口。
2. **启动控制服务**：双击 `启动写字机控制服务.bat`，或 `python grbl_service.py`。只监听 `127.0.0.1:8768`。
3. **设零**：控制台里把笔尖对准屏幕中心，点「设当前点为屏幕中心零点」，或 `python phone_tapper.py set-zero`。`status` 应返回 `Idle` 且 `origin_valid: true`。
4. **标定**：打开控制台 → 手机浏览器打开 `touch_server.py` 显示的采样页 → 点「开始 / 显示亮点」→ 逐点对准、点按并记录 → 至少 3 个不共线点后「拟合并保存」。详见下文。
5. **试一次点按**：`python phone_tapper.py tap 0.5 0.5`，看摄像头画面确认落点。

```powershell
python phone_tapper.py status
python phone_tapper.py goto 0.5 0.5
python phone_tapper.py tap 0.5 0.5
python phone_tapper.py swipe 0.5 0.8 0.5 0.2
```

## 坐标与标定

- `u/v` 是屏幕比例，左上 `(0,0)`、右下 `(1,1)`；**不要拿照片像素当触屏坐标**。
- 标定结果存 `tapper_config.json` 的 `affine`（2×3 矩阵）+ `screen_css_w/h`（采样页视口）。未标定时触控会被拒绝。
- 采样点距屏幕边缘保留 20% 余量，边缘区域靠仿射外推；换手机、换摆位、改夹持后必须重标。
- 手机屏幕不同位置的接触深度不同，用 `z_contact_map` 按 `u/v` 局部记录 `z_rest_mm`；一次只改一个点，别无界加深。
- 仓库里的 `tapper_config.json` 是**未标定模板**（`affine: null`），第一次使用必须自己标。

参考精度：12 点拟合最大误差约 1.1 CSS px，重载后复测中心点约 0.4 px。

## HTTP API

固定绑定本机回环，端口 `8768`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务版本与特性 |
| GET | `/api/status` | Grbl 状态、串口、原点、行程与限位设置 |
| GET | `/api/camera.jpg` | 最新一帧（响应头带帧序号、帧龄、动作 ID） |
| POST | `/api/tap` | `{"u":0.5,"v":0.5}` |
| POST | `/api/tap-sequence` | `{"points":[...]}`，最多 50 点，整串预检后连续执行，末尾只停车一次 |
| POST | `/api/swipe` | `{"u1":..,"v1":..,"u2":..,"v2":..,"duration_ms":600}` |
| POST | `/api/goto` | 抬笔移动到屏幕坐标 |
| POST | `/api/stop` | Grbl feed hold |

服务**不接受任意 G-code**，只有以上有限动作；所有目标都做屏幕范围、机器行程、Z 行程和半径限制检查。

输入一串文字时，把屏幕键盘按键拼成一个 `tap-sequence` 一次打完，中间不停车——每敲一个键回一次停车位会慢十倍，还会改变画面。

## MCP：让 AI 直接操作

`phone_mcp_server.py` 把能力封装成 6 个工具，只连 `127.0.0.1:8768`，自身不开串口、不发 G-code。

- `phone_status`：检查服务、Grbl、原点
- `phone_camera`：最新画面
- `phone_tap` / `phone_tap_sequence` / `phone_swipe`：动作 + 对应新帧
- `phone_stop`：急停

stdio 配置：

```json
{
  "mcpServers": {
    "btwriter-phone": {
      "command": "python",
      "args": ["F:\\path\\to\\btwriter-phone\\phone_mcp_server.py"]
    }
  }
}
```

Streamable HTTP：`python phone_mcp_server.py --http` → `http://127.0.0.1:8769/mcp`。

## 固定流程与 AI 半自动

```powershell
# 重放已校准的 YAML，关键节点用本机 OCR 核对
python workflow_runner.py run flows\wechat_nihao.yaml
# 外发动作要显式授权
python workflow_runner.py run flows\wechat_nihao.yaml --approve-send
```

流程在标题、键盘、草稿或发送后画面不匹配时**停止并保存截图**，不会猜坐标。`flows/` 里的坐标来自一台具体设备，仅作结构示例。

`ai-task` 模式由 AI 每步观察 → 规划 → 执行 → 再取帧复核，需要可选的 `phone_automation` 组件（本地 GUI 模型 + OCR + 透视矫正），通过环境变量指定：

```powershell
$env:PHONE_AUTOMATION_HOME = "D:\path\to\phone_automation"
python workflow_runner.py ai-task "在当前微信聊天发送你好"
```

没有该组件时，固定流程、GUI、HTTP、MCP 全部可用，只有 `ai-task` 不可用。

## 打包便携版

```powershell
python -m PyInstaller --noconfirm --clean BtWriterPhone.spec
```

或双击 `打包便携版.bat`。产物 `dist\BtWriterPhone\` 可整体复制到另一台 Windows 电脑，免装 Python；目标机只需 CH340 驱动和摄像头驱动。

## 安全边界与已知限制

- 控制器 `$20/$21/$22` 都是 0：**软限位、硬限位、回零全部关闭**。程序只靠软件边界检查，上电后必须人工设零。
- 串口断线、Grbl 报警、服务重启都会让原点失效；不会自动回零。
- 屏幕目标半径限制 120 mm，摄像头停靠点 145 mm，未设零点动 30 mm；超出直接拒绝。
- 笔架弹簧回弹：不要手写 Z 上行命令，会打齿。
- 桌面图标可能处于编辑模式，慢速下压会被识别成长按；普通点按用 `z_down_feed: 1200`。
- 滑动必须 Z 下压与 XY **同时**启动，否则起点被按住约一秒，手机判成长按。
- 同名图标（例如两个「微信」）、全局搜索入口、键盘布局切换都是常见陷阱，动作后必须核对页面标题/内容。
- 摄像头同一时刻只能被一个进程占用；控制服务开着时，别的程序（包括另一个服务实例）打不开它。
- 触控频率低，不适合高频批量操作；长时间连续下压会发热、笔头磨损。

## 目录

```
btwriter-phone/
├─ grbl_service.py          # 常驻控制服务：串口 + 运动 + 坐标映射 + HTTP
├─ phone_camera.py          # 摄像头后端（可选替换）
├─ phone_tapper_gui.py      # tkinter 可视化控制台
├─ phone_tapper.py          # 命令行工具
├─ touch_server.py          # 触屏采样页（标定用）
├─ auto_calib.py            # 自动标定（需可选组件）
├─ pressure_calib.py        # Z 接触深度校准
├─ enable_speed_profile.py  # 加速配置切换
├─ test_speed_profile.py    # 离线回归 + 测速
├─ workflow_runner.py       # YAML 固定流程 / AI 半自动
├─ phone_mcp_server.py      # MCP 适配
├─ btwriter_app.py          # 便携版入口
├─ tapper_config.json       # 设备与标定配置（模板）
├─ flows/                   # 流程示例
└─ docs/FIELD-NOTES.md      # 实机踩坑记录
```

## English

Control a phone by physically tapping it with a GRBL plotter's pen. A camera watches the screen, an AI or a YAML workflow plans normalized `u/v` targets, and a loopback-only service (`127.0.0.1:8768`) moves the pen. Every action must be confirmed on a fresh frame — an `ok` response only means the motion finished.

No ADB, no accessibility service, no root, works on Android and iOS alike. The trade-off is speed: roughly 1–3 s per touch. Z is press-only (spring return + `G92`), XY and Z start together for swipes, and the service refuses arbitrary G-code. A ready-to-use MCP server exposes `phone_status`, `phone_camera`, `phone_tap`, `phone_tap_sequence`, `phone_swipe` and `phone_stop` to local AI clients.

Windows only; requires a GRBL plotter, a camera over the phone, and manual re-zeroing after every power cycle.

## 联系与反馈

- Instagram：[liyuai68](https://www.instagram.com/liyuai68/)
- 抖音：李玉ai

有问题欢迎在 Issues 里提，也可以直接私信。

## License

MIT
