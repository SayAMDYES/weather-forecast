# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

本地运行的朝霞 / 晚霞（火烧云）预报 PC 网页：下载 GFS + ECMWF 低/中/高云量预报，沿太阳方向追踪光路打分，在 MapLibre 地图上着色。只做本地使用，不部署、不推送通知。界面文字、错误提示和文档都用中文。

## 命令

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # Python 3.14；运行时依赖在 requirements.txt
.venv/bin/python -m glow serve                    # http://127.0.0.1:8765，启动时不计算任何东西
.venv/bin/python -m glow run [--region <id>]      # 重算已保存区域后退出
.venv/bin/python -m pytest                        # 全部测试（pytest.ini: testpaths = tests）
.venv/bin/python -m pytest tests/test_regions.py::test_name -q   # 单个测试
```

没有 lint / 构建步骤；前端是纯静态文件（`site/`），不打包。`.claude/launch.json` 里的 `glow` 配置用于在内置浏览器中预览。测试不访问网络（数据源用 monkeypatch 或 `tests/data/` 下的样本），`tests/conftest.py` 会屏蔽本机的 `config/local.yaml`；用到 `paths` 的测试要把它们指向 `tmp_path`。GitHub Actions（`.github/workflows/test.yml`）在 push 时跑 pytest。

## 架构

数据流：用户在网页放置区域 → `server.Worker` 后台排队 → `pipeline.run_region` → 写出 `data/output/regions/{id}/` → 服务器以 `/data/` 提供（`Handler.translate_path` 映射到 `paths.output`）→ 前端读取 manifest 和 PNG。运行时数据全在 `data/` 下，本地运行和 Docker（只挂载 `data/` 和只读的 `config/`）共用。

- **配置（`glow/config.py`）**：读 `config/default.yaml`，再按层合并不进 git 的 `config/local.yaml`（列表整体替换；`--config` 指定其他文件时不合并）。`paths` 下的路径相对项目根解析。
- **区域（`glow/regions.py`，存于 `data/regions.json`）**：每个区域是固定大小的显示框（`config/default.yaml` 的 `regions.frame`，当前 4° × 5°、0.02° 网格），由中心点生成。每个区域有两个 Box：`display`（着色范围）和 `compute`（下载云量/地形的范围），后者由 `config.compute_box_for` 按光路长度（`scoring.ray.max_km` + 50 km）和当地纬度下日出日落最大方位角自动外扩。任何读取云量或地形的代码都必须用 `compute` 盒；点详情在 `display` 之外直接拒绝而不是钳位，因为光路可能超出已下载范围。
- **数据源（`glow/sources/`）**：`pipeline.fetch_model` 先用主源（`gfs.py`：NOMADS 子区域 → AWS `.idx` 字节范围；`ecmwf.py`：data.ecmwf.int `.index` 字节范围，0p1 优先），任何异常都回退到 `openmeteo.py`。每个源都实现 `find_run(...)` 和 `load_steps(cfg, run, steps, region, progress)`，返回 `base.CloudField` 列表。缓存路径：`data/cache/{model}/{tag}/{YYYYMMDDHH}/{region_id}/f{step}.npz`（按区域裁剪）；ECMWF 无法服务端裁剪，所以每个起报的全球 GRIB 存在 `.../raw/` 里，供所有区域本地裁剪。`pipeline.cleanup_cache` 删除 `forecast.keep_days` 天前的起报和已删除区域的数据。
- **地形（`glow/terrain.py`）**：Copernicus GLO-90 COG 通过 GDAL `/vsicurl/` 读概览层，瓦片地址是 `terrain.tile_url`，按 1° 瓦片缓存到 `data/cache/dem/`（最大值用于遮挡，平均值作观测点高度）。
- **评分（`glow/score.py`，纯函数，参数全部来自 YAML `scoring`）**：上空云幕项（canopy）+ 沿方位角在多个仰角 α 上追踪光线（含地球曲率和本地 `self_clearance_km`）的透过率 + 观测项（含云海），最后组合并给出原因代码（`explain`）。`pipeline.score_event` 在两个预报时次之间按目标时刻逐点插值；`detail.point_detail` 用同一套代码给单点算完整拆解，保证与地图一致。
- **太阳（`glow/sun.py`）**：astral 计算，用区域的 IANA 时区（`regions.timezone_at`，海上回退 `Etc/GMT±N`）；全区域先算 0.1° 粗网格再双线性插值。
- **收藏机位（`glow/spots.py`，存于 `data/spots.json`）**：网页上添加/删除，名称唯一，所有区域共用。manifest 写入计算时落在框内的机位，只供历史存档使用；当前区域的总览表由前端用 `/api/regions` 返回的最新列表按显示框过滤（`viewSpots()`），所以增删机位不需要重算。
- **输出（`glow/output.py` / `pipeline.run_region`）**：每个区域一个 `manifest.json`（起报信息、机位、每日日出/日落事件及各模式的灰度 PNG 路径）。JSON 原子写入。
- **服务器（`glow/server.py`）**：标准库 `ThreadingHTTPServer`。`GET /api/status`、`/api/regions`（含 `limits`、`spots` 和 `map`）、`/api/point`（按需计算点详情，`Details` 按 manifest 的 `generated_at` 缓存 Context）；`POST /api/refresh`、`POST /api/regions {name, lat, lon}`、`DELETE /api/regions/{id}`、`POST /api/spots {name, lat, lon}`、`DELETE /api/spots/{id}`、`DELETE /api/archive/...`。改状态的请求要求 Host 在 `server.allowed_hosts` 内（防 DNS 重绑定），且 Origin 等于 Host。Worker 单线程、队列去重；区域在计算中被删除时会清掉刚写出的结果。
- **前端（`site/app.js`）**：MapLibre GL 5.24.0 UMD（新版只有 ESM）+ 底图样式 `map.style_url`（默认 OpenFreeMap 深色）；CDN 资源带 SRI，升级版本时要重算 `integrity`。地图在拿到 `/api/regions` 后才创建，因为样式地址和初始视野来自配置。灰度 PNG 按 Web Mercator 行重采样后作为 image source 着色；缩小到区域适配级别以下时隐藏着色层。区域框尺寸从 `/api/regions` 的 `limits.frame` 读取，不要在前端写死。

设计过程文件只保留在本地，已在 `.gitignore` 中排除，不进仓库：`docs/implementation-plan.md`（数据源核实结果和设计取舍）、`.design/`、最初的需求 `plan.md` 和早期演示页 `reference/`。`docs/` 里只提交 README 用到的文件（如 `screenshot.png`）。

## 约定

- 所有可调参数（评分、数据源 URL、重试、区域框、底图）放在 `config/default.yaml`，代码里不写死；个人修改放 `config/local.yaml`。用户数据（区域、收藏机位）存在 `data/` 下的 JSON 里，不放配置。
- 数据源的已知坑：eccodes 的 `typeOfFirstFixedSurface` 要用 `codes_get(..., ktype=str)` 读；GFS NOMADS 同时返回瞬时值和 6 小时平均值，只用瞬时值；Open-Meteo 会 429 限流，`sources/http.get` 按 Retry-After 等待。
- 作者署名一律用 `Quasar`。
