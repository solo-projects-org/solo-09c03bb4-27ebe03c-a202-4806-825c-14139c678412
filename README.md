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

数据卷 `deid-data` 持久化:SQLite 映射库(`deid.db`)、结果包(`output/`)、患者代号密钥(`secret.key`)。删除密钥会导致同一患者得到新代号,请妥善备份。

## API 用法

```bash
# 1. 上传 CT ZIP,创建批次(202 立即返回,后台异步处理)
curl -F "file=@sample_data/sample_ct.zip" http://localhost:8000/api/batches
# => {"batch_id":"...","status":"PENDING","status_url":"/api/batches/..."}

# 2. 轮询批次状态(PENDING/PROCESSING/COMPLETED/REJECTED/FAILED)
curl http://localhost:8000/api/batches/<batch_id>

# 3. 完成后下载去标识化包
curl -OJ http://localhost:8000/api/batches/<batch_id>/download
```

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
- **失败清理**:失败/拒收批仅删除**本批独占**的 UID 映射与半成品文件;患者代号等全局映射不动,不影响其他成功批。处理结束(无论成败)即删除原始上传包。
- **重启恢复**:启动时扫描状态为 `PENDING/PROCESSING` 的批次,一律标记 `FAILED` 并清理其独占映射与残留文件;`COMPLETED` 批次的结果包仍在数据卷中,可继续下载。
- **并发安全**:SQLite 开启 WAL + 忙等待,单连接全局锁串行化写事务;患者代号登记用 `INSERT OR IGNORE` + 重读,并发批登记同一患者结果一致。

## 本地开发

```bash
pip install -r requirements.txt httpx
DATA_DIR=/tmp/deid-data uvicorn app.main:app --port 8000
python scripts/smoke_test.py   # 端到端冒烟测试(需服务已启动)
```

## 目录结构

```
app/
  main.py        FastAPI 路由与生命周期(上传/查询/下载,启动恢复)
  processing.py  批次处理:校验、UID 重编、去标识化、打包、登记、清理
  db.py          SQLite 映射库(patients / uid_map / batches)
  config.py      数据卷路径与常量
scripts/
  make_sample_ct.py  生成虚构 CT ZIP(含嵌套引用)
  smoke_test.py      端到端冒烟测试
sample_data/     预生成的样本包
Dockerfile / docker-compose.yml / requirements.txt
```
