"""批次处理核心:校验 -> 重编 UID -> 去标识化 -> 打包 -> 登记映射 -> 残件清理。

设计要点:
- 校验(缺患者号 / 包外实例引用 / BurnedInAnnotation != NO)任一不满足,整批拒收。
- 患者号 -> 稳定代号(HMAC,密钥持久化在数据卷,跨批/跨重启一致)。
- 检查/序列/实例 UID 全部重编;按"值"递归替换,序列内嵌套引用同步改写;
  SOP Class UID 与 Transfer Syntax UID 不在映射表内,天然保持不变;像素数据不触碰。
- 完整包落盘 + 映射登记在同一先后序列里都成功,批次才置为 COMPLETED 供下载。
- 残件清理(失败/中断批的原包、临时目录、未发布结果、独占映射)以
  cleanup_targets 表为持久化"待清理依据":删除成功才标记 DONE;
  权限错误等删除失败会保留依据,由后台补清线程自动重试,服务可正常启动,
  故障解除后无须重启即补清。清单路径即白名单,补清绝不删除新批次的在途文件。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import shutil
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset

from . import db
from .config import (
    CLEANUP_INTERVAL,
    CT_IMAGE_STORAGE,
    EXPLICIT_VR_LITTLE_ENDIAN,
    FAULT_DIR,
    FAULT_INJECTION,
    OUTPUT_DIR,
    SECRET_PATH,
    TMP_DIR,
    UPLOAD_DIR,
)

log = logging.getLogger("deid.processing")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_uid() -> str:
    """2.25 开头的 UUID 十进制根,合法且永不碰撞。"""
    return "2.25." + str(uuid.uuid4().int)


class BatchRejected(Exception):
    """业务校验失败 -> 整批拒收(REJECTED),消息可直接返回给调用方。"""


# ---------------------------------------------------------------- 容错删除

def _fault(name: str) -> None:
    """演示/测试用故障注入:存在标记文件时模拟一次权限错误。"""
    if FAULT_INJECTION and (FAULT_DIR / name).exists():
        raise PermissionError(f"[fault-inject] 模拟权限拒绝: {name}")


def remove_file(path: Path) -> None:
    """删除单个文件;文件已不存在视为成功(删除天然幂等)。"""
    _fault("delete_file")
    try:
        path.unlink()
    except FileNotFoundError:
        return


def remove_tree(path: Path) -> None:
    """删除整棵临时目录;目录已不存在视为成功。"""
    _fault("rmtree")
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return


# ---------------------------------------------------------------- 密钥 / 患者代号

_secret: bytes | None = None


def _load_secret() -> bytes:
    global _secret
    if _secret is not None:
        return _secret
    if SECRET_PATH.exists():
        _secret = SECRET_PATH.read_bytes()
    else:
        _secret = secrets.token_bytes(32)
        SECRET_PATH.write_bytes(_secret)
        os.chmod(SECRET_PATH, 0o600)
    return _secret


def patient_codename(original_patient_id: str) -> str:
    """同一原始患者号永远得到同一代号(跨批、并发、重启一致)。"""
    digest = hmac.new(_load_secret(), original_patient_id.encode("utf-8"), hashlib.sha256)
    return "PT-" + digest.hexdigest()[:12].upper()


def get_or_create_patient(original_patient_id: str) -> str:
    row = db.query_one(
        "SELECT codename FROM patients WHERE original_patient_id = ?", (original_patient_id,)
    )
    if row:
        return row["codename"]
    code = patient_codename(original_patient_id)
    with db.tx() as c:
        # 并发批可能同时登记同一患者:INSERT OR IGNORE 后再读,保证一致
        c.execute(
            "INSERT OR IGNORE INTO patients(original_patient_id, codename, created_at) VALUES (?,?,?)",
            (original_patient_id, code, utcnow()),
        )
    row = db.query_one(
        "SELECT codename FROM patients WHERE original_patient_id = ?", (original_patient_id,)
    )
    return row["codename"]


# ---------------------------------------------------------------- DICOM 遍历

def iter_datasets(ds: Dataset):
    """深度优先遍历数据集及其所有序列条目。"""
    yield ds
    for elem in ds:
        if elem.VR == "SQ":
            for item in elem.value:
                yield from iter_datasets(item)


def collect_referenced_instance_uids(ds: Dataset) -> set[str]:
    """收集数据集(含嵌套序列)里所有"实例级引用"的 SOP Instance UID。

    顶层数据集自身的 SOPInstanceUID 不算引用;其余任何关键字里带
    SOPInstanceUID 的 UI 元素(如 ReferencedSOPInstanceUID)都视为实例引用。
    """
    refs: set[str] = set()
    for d in iter_datasets(ds):
        for elem in d:
            if elem.VR != "UI" or "SOPInstanceUID" not in elem.keyword:
                continue
            if d is ds and elem.keyword == "SOPInstanceUID":
                continue
            values = elem.value if elem.VM > 1 else [elem.value]
            for v in values:
                v = str(v or "").strip()
                if v:
                    refs.add(v)
    return refs


def remap_uids(ds: Dataset, uid_map: dict[str, str]) -> None:
    """按值递归替换:凡是映射表里的 UID(检查/序列/实例),无论嵌套多深都同步改写。

    SOP Class UID / Transfer Syntax UID 等不在映射表中,原样保留。
    """
    for d in iter_datasets(ds):
        for elem in d:
            if elem.VR != "UI":
                continue
            if elem.VM > 1:
                elem.value = [uid_map.get(str(v), str(v)) for v in elem.value]
            else:
                v = str(elem.value)
                if v in uid_map:
                    elem.value = uid_map[v]


# ---------------------------------------------------------------- 校验

def validate_file(ds: Dataset, name: str) -> None:
    ts = str(getattr(ds.file_meta, "TransferSyntaxUID", "") or "")
    if ts != EXPLICIT_VR_LITTLE_ENDIAN:
        raise BatchRejected(f"{name}: 传输语法不是显式小端非压缩(实际 {ts or '缺失'})")
    sop_class = str(getattr(ds, "SOPClassUID", "") or "")
    if sop_class != CT_IMAGE_STORAGE:
        raise BatchRejected(f"{name}: SOP Class 不是 CT Image Storage(实际 {sop_class or '缺失'})")
    if not str(getattr(ds, "SOPInstanceUID", "") or "").strip():
        raise BatchRejected(f"{name}: 缺少 SOP Instance UID")
    if not str(getattr(ds, "PatientID", "") or "").strip():
        raise BatchRejected(f"{name}: 缺少患者号 PatientID,整批拒收")
    burned = str(getattr(ds, "BurnedInAnnotation", "NO") or "NO").strip().upper()
    if burned != "NO":
        raise BatchRejected(f"{name}: BurnedInAnnotation={burned}(非 NO),整批拒收")


# ---------------------------------------------------------------- 解包 / 打包

def extract_zip(zip_path: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BatchRejected("上传内容不是有效的 ZIP 压缩包")
    files: list[Path] = []
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = Path(info.filename).name
            if not base or base.startswith(".") or "__MACOSX" in info.filename:
                continue
            # 防路径穿越:只取文件名,统一平铺到 dest
            target = dest / f"{len(files):06d}_{base}"
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            files.append(target)
    if not files:
        raise BatchRejected("压缩包内没有任何文件")
    return files


def build_output_zip(src_dir: Path, batch_id: str) -> Path:
    """打包到临时文件后原子改名,避免半截包被下载。"""
    tmp_zip = OUTPUT_DIR / f"{batch_id}.zip.tmp"
    final_zip = OUTPUT_DIR / f"{batch_id}.zip"
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_dir.iterdir()):
            zf.write(f, arcname=f.name)
    with open(tmp_zip, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp_zip, final_zip)
    return final_zip


# ---------------------------------------------------------------- 批次状态

def _set_status(batch_id: str, status: str, **fields) -> None:
    cols = {"status": status, "updated_at": utcnow(), **fields}
    sql = "UPDATE batches SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE batch_id = ?"
    with db.tx() as c:
        c.execute(sql, tuple(cols.values()) + (batch_id,))


def _artifact_targets(batch_id: str, include_output: bool) -> list[tuple[str, str]]:
    """本批次可能残留的磁盘工件清单(清理白名单)。

    include_output=False(成功批):只清原包与临时目录,结果包必须保留可下载。
    include_output=True (失败/中断批):连同未发布结果(含 .tmp 半成品)一起清。
    """
    targets = [
        (str(UPLOAD_DIR / f"{batch_id}.zip"), "FILE"),
        (str(TMP_DIR / batch_id), "DIR"),
    ]
    if include_output:
        targets += [
            (str(OUTPUT_DIR / f"{batch_id}.zip"), "FILE"),
            (str(OUTPUT_DIR / f"{batch_id}.zip.tmp"), "FILE"),
        ]
    return targets


def enqueue_cleanup(batch_id: str, include_output: bool = True) -> int:
    """把本批残件登记进持久化清理清单并唤醒补清线程。返回登记条数。

    已存在的条目(UNIQUE(batch_id, path))原样保留,因此失败重登记、
    重启对账重复执行都不会丢掉待清理依据,也不会重置已完成的条目。
    """
    now = utcnow()
    with db.tx() as c:
        before = c.execute(
            "SELECT COUNT(*) FROM cleanup_targets WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        c.executemany(
            "INSERT OR IGNORE INTO cleanup_targets"
            "(batch_id, path, kind, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            [(batch_id, p, k, "PENDING", now, now)
             for (p, k) in _artifact_targets(batch_id, include_output)],
        )
        total = c.execute(
            "SELECT COUNT(*) FROM cleanup_targets WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        remaining = c.execute(
            "SELECT COUNT(*) FROM cleanup_targets"
            " WHERE batch_id = ? AND status != 'DONE'", (batch_id,)
        ).fetchone()[0]
        # 只要还有未删完的工件,批次清理进度就保持未完成
        c.execute(
            "UPDATE batches SET cleanup_status = ?, cleanup_total = ?,"
            " cleanup_remaining = ?, cleaned_at = NULL, updated_at = ? WHERE batch_id = ?",
            ("PENDING" if remaining else "DONE", total, remaining, now, batch_id),
        )
        added = total - before
    sweeper.wake()
    return added


def _fail(batch_id: str, status: str, reason: str) -> None:
    """失败/拒收:独占 UID 映射随状态事务清除,磁盘残件交给持久化清理清单。

    患者代号等全局映射不动,不影响其他成功批。即使删除持续报权限错误,
    批次仍能正确标记为 REJECTED/FAILED,服务启动与其他批次操作不受影响。
    """
    log.warning("批次 %s %s: %s", batch_id, status, reason)
    with db.tx() as c:
        c.execute("DELETE FROM uid_map WHERE batch_id = ?", (batch_id,))
        c.execute(
            "UPDATE batches SET status = ?, error = ?, updated_at = ? WHERE batch_id = ?",
            (status, reason, utcnow(), batch_id),
        )
    enqueue_cleanup(batch_id)


# ---------------------------------------------------------------- 主流程

def process_batch(batch_id: str) -> None:
    upload_path = UPLOAD_DIR / f"{batch_id}.zip"
    work_dir = TMP_DIR / batch_id
    try:
        _set_status(batch_id, "PROCESSING")

        # 1. 解包并逐个读取校验
        files = extract_zip(upload_path, work_dir / "in")
        datasets: list[tuple[str, Dataset]] = []
        for f in files:
            try:
                ds = pydicom.dcmread(str(f))
            except Exception:
                raise BatchRejected(f"{f.name}: 不是可解析的 DICOM 文件")
            validate_file(ds, f.name)
            datasets.append((f.name, ds))

        # 2. 汇总包内 UID;实例 UID 不得重复
        study_uids = {str(ds.StudyInstanceUID) for _, ds in datasets if "StudyInstanceUID" in ds}
        series_uids = {str(ds.SeriesInstanceUID) for _, ds in datasets if "SeriesInstanceUID" in ds}
        sop_list = [str(ds.SOPInstanceUID) for _, ds in datasets]
        if len(sop_list) != len(set(sop_list)):
            raise BatchRejected("压缩包内存在重复的 SOP Instance UID")
        sop_uids = set(sop_list)

        # 3. 包外实例引用检查:任何嵌套引用的实例都必须在本包内
        for name, ds in datasets:
            outside = collect_referenced_instance_uids(ds) - sop_uids
            if outside:
                raise BatchRejected(
                    f"{name}: 引用了压缩包外的实例 {sorted(outside)},整批拒收"
                )

        # 4. 建立映射:患者号(全局稳定)+ 本批独占的检查/序列/实例 UID
        patient_ids = {str(ds.PatientID).strip() for _, ds in datasets}
        codenames = {pid: get_or_create_patient(pid) for pid in patient_ids}

        uid_map: dict[str, str] = {}
        uid_rows: list[tuple[str, str, str, str]] = []  # (orig, new, type)
        for u in sorted(study_uids):
            uid_map[u] = new_uid(); uid_rows.append((u, uid_map[u], "STUDY"))
        for u in sorted(series_uids):
            uid_map[u] = new_uid(); uid_rows.append((u, uid_map[u], "SERIES"))
        for u in sorted(sop_uids):
            uid_map[u] = new_uid(); uid_rows.append((u, uid_map[u], "INSTANCE"))

        # 5. 去标识化 + 递归改 UID + 落盘(文件名 = 新实例 UID)
        out_dir = work_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        for _, ds in datasets:
            old_sop = str(ds.SOPInstanceUID)
            ds.PatientID = codenames[str(ds.PatientID).strip()]
            ds.PatientName = ""
            ds.PatientBirthDate = ""
            ds.AccessionNumber = ""
            ds.remove_private_tags()
            remap_uids(ds, uid_map)
            # 文件元信息同步:实例 UID 换新,SOP Class / 传输语法保持不变
            ds.file_meta.MediaStorageSOPInstanceUID = uid_map[old_sop]
            ds.save_as(str(out_dir / f"{uid_map[old_sop]}.dcm"), write_like_original=False)

        # 6. 打包(完整包先落盘)
        final_zip = build_output_zip(out_dir, batch_id)

        # 7. 映射登记 + 状态置为 COMPLETED,同一事务;两者都成功才供下载
        now = utcnow()
        with db.tx() as c:
            c.executemany(
                "INSERT INTO uid_map(batch_id, original_uid, new_uid, uid_type, created_at)"
                " VALUES (?,?,?,?,?)",
                [(batch_id, o, n, t, now) for (o, n, t) in uid_rows],
            )
            c.execute(
                "UPDATE batches SET status = 'COMPLETED', file_count = ?,"
                " completed_at = ?, updated_at = ? WHERE batch_id = ?",
                (len(datasets), now, now, batch_id),
            )
        log.info("批次 %s 完成:%d 个实例 -> %s", batch_id, len(datasets), final_zip.name)

        # 8. 成功批:结果包与映射保留,仅清原包与临时目录(同样走持久化清单)
        enqueue_cleanup(batch_id, include_output=False)

    except BatchRejected as e:
        _fail(batch_id, "REJECTED", str(e))
    except Exception as e:  # noqa: BLE001 - 任何意外都不能拖垮工作线程
        log.exception("批次 %s 处理异常", batch_id)
        _fail(batch_id, "FAILED", f"内部错误: {e}")


# ---------------------------------------------------------------- 后台补清

class CleanupSweeper:
    """后台补清线程:按节拍重试清理清单中未完成的条目。

    - 独立守护线程,慢清理/反复失败只延迟本批残件删除,不占工作线程、不阻塞 API
      与其他批次的上传/查询/下载/处理。
    - 每条依据单独尝试、单独记账;实际删完才置 DONE,批次据此才报清理完成。
    - 进程中途退出后,清单仍在 SQLite 里,下次启动由对账逻辑续清。
    """

    def __init__(self, interval: float = CLEANUP_INTERVAL):
        self.interval = max(0.05, interval)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="cleanup-sweeper", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception:  # noqa: BLE001 - 补清线程永不因异常退出
                log.exception("补清轮询发生异常,将在下一节拍重试")
            self._wake.wait(timeout=self.interval)
            self._wake.clear()

    def sweep_once(self) -> int:
        """执行一轮补清。同一时刻只有一轮在跑;返回本轮处理的批次数。"""
        if not self._run_lock.acquire(blocking=False):
            return 0
        try:
            self._reconcile_terminal_batches()
            rows = db.query(
                "SELECT DISTINCT batch_id FROM cleanup_targets WHERE status != 'DONE'"
            )
            for r in rows:
                self._sweep_batch(r["batch_id"])
            return len(rows)
        finally:
            self._run_lock.release()

    def _reconcile_terminal_batches(self) -> None:
        """对账:失败/拒收批盘上多出的未发布结果(外部误放、历史遗留)也要清掉。

        只处理已有终态的批次及其 batch_id 命名的文件,不做通配扫描,
        因此绝不会触碰在途批次或成功批的结果。
        """
        rows = db.query("SELECT batch_id FROM batches WHERE status IN ('REJECTED','FAILED')")
        for r in rows:
            bid = r["batch_id"]
            for p in (OUTPUT_DIR / f"{bid}.zip", OUTPUT_DIR / f"{bid}.zip.tmp"):
                if not p.exists():
                    continue
                now = utcnow()
                with db.tx() as c:
                    # 同路径可能已有历史 DONE 条目(文件被外部重新放回):
                    # 先插入,再把该路径条目重新打开为 PENDING,确保补清会再删一次
                    c.execute(
                        "INSERT OR IGNORE INTO cleanup_targets"
                        "(batch_id, path, kind, status, created_at, updated_at)"
                        " VALUES (?,?, 'FILE', 'PENDING', ?, ?)",
                        (bid, str(p), now, now),
                    )
                    c.execute(
                        "UPDATE cleanup_targets SET status = 'PENDING', last_error = NULL,"
                        " updated_at = ? WHERE batch_id = ? AND path = ? AND status = 'DONE'",
                        (now, bid, str(p)),
                    )
                    c.execute(
                        "UPDATE batches SET cleanup_status = 'PENDING', cleaned_at = NULL,"
                        " updated_at = ? WHERE batch_id = ? AND cleanup_status = 'DONE'",
                        (now, bid),
                    )

    def _sweep_batch(self, batch_id: str) -> None:
        targets = db.query(
            "SELECT * FROM cleanup_targets WHERE batch_id = ? AND status != 'DONE'"
            " ORDER BY id",
            (batch_id,),
        )
        if not targets:
            return
        db.query(  # 批次清理进度 -> IN_PROGRESS
            "UPDATE batches SET cleanup_status = 'IN_PROGRESS', updated_at = ?"
            " WHERE batch_id = ? AND cleanup_status != 'DONE'",
            (utcnow(), batch_id),
        )
        for t in targets:
            self._attempt_target(t)
        self._refresh_batch(batch_id)

    def _attempt_target(self, target) -> None:
        tid = target["id"]
        path = Path(target["path"])
        now = utcnow()
        try:
            if target["kind"] == "DIR":
                remove_tree(path)
            else:
                remove_file(path)
        except Exception as e:  # noqa: BLE001 - 权限错误等:保留依据,下轮重试
            log.warning("清理失败(将自动重试) target=%s: %s", path, e)
            with db.tx() as c:
                c.execute(
                    "UPDATE cleanup_targets SET status = 'FAILED', attempts = attempts + 1,"
                    " last_error = ?, updated_at = ? WHERE id = ?",
                    (f"{type(e).__name__}: {e}", now, tid),
                )
                c.execute(
                    "UPDATE batches SET cleanup_status = 'FAILED', cleanup_error = ?,"
                    " updated_at = ? WHERE batch_id = ?",
                    (f"{type(e).__name__}: {e}", now, target["batch_id"]),
                )
            return
        with db.tx() as c:
            c.execute(
                "UPDATE cleanup_targets SET status = 'DONE', last_error = NULL,"
                " updated_at = ? WHERE id = ?",
                (now, tid),
            )

    def _refresh_batch(self, batch_id: str) -> None:
        now = utcnow()
        row = db.query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status != 'DONE' THEN 1 ELSE 0 END) AS remaining"
            " FROM cleanup_targets WHERE batch_id = ?",
            (batch_id,),
        )
        total, remaining = row["total"], row["remaining"] or 0
        if remaining:
            err = db.query_one(
                "SELECT last_error FROM cleanup_targets WHERE batch_id = ?"
                " AND status != 'DONE' ORDER BY updated_at DESC LIMIT 1",
                (batch_id,),
            )
            with db.tx() as c:
                c.execute(
                    "UPDATE batches SET cleanup_status = 'FAILED', cleanup_total = ?,"
                    " cleanup_remaining = ?, cleanup_error = ?, updated_at = ?"
                    " WHERE batch_id = ?",
                    (total, remaining, err["last_error"] if err else None, now, batch_id),
                )
        else:
            with db.tx() as c:
                c.execute(
                    "UPDATE batches SET cleanup_status = 'DONE', cleanup_total = ?,"
                    " cleanup_remaining = 0, cleanup_error = NULL, cleaned_at = ?,"
                    " updated_at = ? WHERE batch_id = ?",
                    (total, now, now, batch_id),
                )
            log.info("批次 %s 残件已全部清理完成", batch_id)


sweeper = CleanupSweeper()


# ---------------------------------------------------------------- 重启恢复 / 旧卷对账

def recover_and_reconcile() -> None:
    """启动时对账,兼容已有数据卷(含已标失败但仍有残件的批次)。

    分两类:
    1. PENDING/PROCESSING(上次进程中断):标 FAILED、清独占映射,全部残件入清理清单。
    2. REJECTED/FAILED(旧版本遗留,可能标记在前、残件未删):补登记清理清单续清。
    COMPLETED 批的结果包与映射一律保留。

    清理依据只按批次精确登记(路径白名单),不做目录通配清扫,
    因此绝不会误删重启后新上传批次的在途文件。
    """
    now = utcnow()

    interrupted = db.query("SELECT batch_id FROM batches WHERE status IN ('PENDING','PROCESSING')")
    for r in interrupted:
        bid = r["batch_id"]
        with db.tx() as c:
            c.execute("DELETE FROM uid_map WHERE batch_id = ?", (bid,))
            c.execute(
                "UPDATE batches SET status = 'FAILED', error = ?,"
                " updated_at = ? WHERE batch_id = ?",
                ("服务重启,批次中断,未发布", now, bid),
            )
        enqueue_cleanup(bid)
        log.info("重启恢复:中断批次 %s 标记为 FAILED 并登记清理", bid)

    legacy = db.query(
        "SELECT b.batch_id FROM batches b WHERE b.status IN ('REJECTED','FAILED')"
        " AND NOT EXISTS ("
        "  SELECT 1 FROM cleanup_targets t WHERE t.batch_id = b.batch_id)"
    )
    for r in legacy:
        bid = r["batch_id"]
        # 旧卷里可能已残留本批独占映射,幂等清除
        with db.tx() as c:
            c.execute("DELETE FROM uid_map WHERE batch_id = ?", (bid,))
        enqueue_cleanup(bid)
        log.info("旧卷对账:失败批次 %s 补登记清理清单", bid)

    # 已登记过清单的批次(含清理中途退出的)无需额外动作,
    # 未完成条目由 sweeper 依据 SQLite 中的依据自动续清。
    sweeper.wake()
