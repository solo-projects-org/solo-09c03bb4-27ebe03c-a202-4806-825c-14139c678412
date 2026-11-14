# CT 影像去标识化服务

面向影像研究数据员的院内 CT 去标识化 API:上传**显式小端(Explicit VR Little Endian)非压缩 CT** 的 ZIP 包,服务异步完成去标识化并输出改写包,供分割流水线读取;原始标识与新标识的映射登记在 SQLite 映射库中,留存于数据卷供院内回溯。

## 去标识化规则

| 项目 | 处理 |
|---|---|
| 患者姓名 PatientName | 清空 |
| 出生日期 PatientBirthDate | 清空 |
| 检查号 AccessionNumber | 清空 |
| 私有标签 | 全部删除 |
| 患者号 PatientID | 替换为**稳定代号**(`PT-` + HMAC-SHA256,同一患者跨批、跨重启一致) |
| 检查/序列/实例 UID | 全部重编(`2.25.` + UUID),文件元信息与**任意深度嵌套的实例引用**同步替换 |
| SOP Class UID / Transfer Syntax UID | **不变** |
| 像素数据 | **不变**(逐字节一致) |

**整批拒收**(状态 `REJECTED`,不产生任何输出):

- 任一文件缺少患者号 PatientID
- 任一文件存在指向压缩包外实例的引用(嵌套序列中的 `ReferencedSOPInstanceUID` 等)
- 任一文件 `BurnedInAnnotation` 不为 `NO`
- 非 ZIP、空 ZIP、非 DICOM 文件、非显式小端、非 CT Image Storage、实例 UID 重复

**输出包**:文件以新实例 UID 命名(`<新SOPInstanceUID>.dcm`),包内不含任何原标识对照。

## 快速启动

```bash
docker compose up --build
# Swagger UI: http://localhost:8000/docs
```

数据卷 `deid-data` 持久化:SQLite 映射库(`deid.db`,含批次状态、UID/患者映射与清理清单)、结果包(`output/`)、患者代号密钥(`secret.key`)。删除密钥会导致同一患者得到新代号,请妥善备份。

## API 用法

```bash
# 1. 上传 CT ZIP,创建批次(202 立即返回,后台异步处理)
curl -F "file=@sample_data/sample_ct.zip" http://localhost:8000/api/batches
# => {"batch_id":"...","status":"PENDING","status_url":"/api/batches/..."}

# 2. 轮询批次状态
#    status:处理结局 PENDING/PROCESSING/COMPLETED/REJECTED/FAILED
#    cleanup_status:清理进度 NOT_REQUIRED/PENDING/IN_PROGRESS/DONE/FAILED
curl http://localhost:8000/api/batches/<batch_id>

# 3. 完成后下载去标识化包(未发布批返回 409)
curl -OJ http://localhost:8000/api/batches/<batch_id>/download
```

## 故障演示(权限错误 / 清理中途退出)

`scripts/demo_cleanup_recovery.py` 在独立临时目录上启动真实 uvicorn,
用 `DATA_DIR/faults/` 标记文件模拟卷权限导致的删除失败(等价 `PermissionError`),
完整演示两个场景,结束后自动清理临时目录:

```bash
pip install -r requirements.txt
python3 scripts/demo_cleanup_recovery.py
# 场景 A:权限故障 -> 批次照常失败、残件与清理依据保留 -> 其他批次不受阻塞
#         -> 恢复权限(删标记) -> 不重启,后台自动补清到 DONE
# 场景 B:清理中途杀掉进程 -> 重启 -> 依据仍在,自动续清;成功批结果与映射保留
python3 scripts/demo_cleanup_recovery.py --keep   # 保留临时数据卷便于人工查阅
```

> 故障注入由 `DEID_FAULT_INJECTION=1` 开启,仅用于本地演示/测试;
> Docker Compose 默认关闭。真实权限故障(只读数据卷)的处理路径与注入完全一致。

## 生成虚构 CT 样本

```bash
pip install -r requirements.txt
python scripts/make_sample_ct.py --out sample_data/sample_ct.zip        # 正常:3 层,含嵌套实例引用
python scripts/make_sample_ct.py --out bad1.zip --external-ref          # 包外实例引用 -> 拒收
python scripts/make_sample_ct.py --out bad2.zip --burned-in             # BurnedInAnnotation=YES -> 拒收
python scripts/make_sample_ct.py --out bad3.zip --no-patient-id         # 缺患者号 -> 拒收
```

样本每层含:`ReferencedImageSequence`(引用前一层)、`ReferencedSeriesSequence` → `ReferencedSOPSequence`(嵌套列出全序列实例)、`ReferencedStudySequence`、私有标签、`BurnedInAnnotation=NO`。

## 一致性与故障语义

- **并行**:`DEID_WORKERS`(默认 2)个工作线程,两批可并行处理,一批慢打包不阻塞另一批。
- **发布原子性**:结果包先整体落盘(临时文件 + 原子改名),随后映射登记与批次状态在**同一 SQLite 事务**中提交;两者都成功才置为 `COMPLETED` 供下载。
- **处理结局与清理进度分离**:`status` 表示处理结局(`PENDING/PROCESSING/COMPLETED/REJECTED/FAILED`),`cleanup_status` 表示残件清理进度(`NOT_REQUIRED/PENDING/IN_PROGRESS/DONE/FAILED`),查询接口两者独立返回,可分别筛选;另附 `cleanup_total/cleanup_remaining/cleanup_error/cleaned_at`。
- **持久化清理清单**:失败/拒收/中断批需要清除的每件残件(原包、临时目录、未发布结果及 `.tmp` 半成品)都登记在 `cleanup_targets` 表作为"待清理依据",**实际删完才标记 DONE**,批次据此才报清理完成。删除遇到权限错误时依据不丢失,条目置 `FAILED` 并记录原因,由后台补清线程按 `DEID_CLEANUP_INTERVAL`(默认 5 秒)节拍自动重试。
- **失败批清理范围**:清掉本批独占的 UID 映射与全部磁盘残件;患者代号等全局映射不动。成功批仅清原包与临时目录,**结果包与独占映射保留**可下载。
- **服务可启动、免重启自愈**:清理异常不影响 API 启动;卷权限恢复后补清线程自动清完,无须再次重启。慢清理与反复失败只延迟本批残件删除,不占用工作线程,不阻塞其他批次的上传、查询、下载与处理。
- **白名单式补清**:后台线程只删除 `cleanup_targets` 中按批次精确登记的路径,绝不做目录通配清扫,因此不会误删重启后新上传批次的在途文件。
- **重启恢复 / 旧卷兼容**:启动时把 `PENDING/PROCESSING` 的中断批标记 `FAILED`、清独占映射并登记清理;对旧版本数据卷中"已标 `REJECTED/FAILED` 但残件仍在"的批次自动补登记续清(旧库自动迁移新增列与清理表)。`COMPLETED` 批的结果包与映射跨重启保留。
- **未发布包不可下载**:下载接口仅对 `COMPLETED` 且文件在位的批次放行;`REJECTED/FAILED` 批即使盘上残留同名文件也返回 409,且残件随后被补清。
- **并发安全**:SQLite 开启 WAL + 忙等待,单连接全局锁串行化写事务;患者代号登记用 `INSERT OR IGNORE` + 重读,并发批登记同一患者结果一致。

## 本地开发

```bash
pip install -r requirements.txt httpx pytest
DATA_DIR=/tmp/deid-data uvicorn app.main:app --port 8000
DEID_BASE=http://127.0.0.1:8000 python scripts/smoke_test.py   # 端到端冒烟测试(需服务已启动)
python -m pytest tests/ -q                                     # 清理恢复专项测试(无需起服务)
```

## 目录结构

```
app/
  main.py        FastAPI 路由与生命周期(上传/查询/下载,启动对账,补清线程启停)
  processing.py  批次处理:校验、UID 重编、去标识化、打包、登记;清理清单与后台补清
  db.py          SQLite 映射库(patients / uid_map / batches / cleanup_targets + 旧卷迁移)
  config.py      数据卷路径、补清间隔与故障注入开关
scripts/
  make_sample_ct.py           生成虚构 CT ZIP(含嵌套引用)
  smoke_test.py               端到端冒烟测试(去标识规则/拒收/并行/清理)
  demo_cleanup_recovery.py    权限故障与清理中途退出的本地演示
tests/
  conftest.py                 临时数据卷与 TestClient 夹具
  test_cleanup_recovery.py    清理恢复专项 pytest 用例
sample_data/     预生成的样本包
Dockerfile / docker-compose.yml / requirements.txt
```
