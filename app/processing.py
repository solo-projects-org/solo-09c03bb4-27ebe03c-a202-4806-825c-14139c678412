"""批次处理核心:校验 -> 重编 UID -> 去标识化 -> 打包 -> 登记映射。

设计要点:
- 校验(缺患者号 / 包外实例引用 / BurnedInAnnotation != NO)任一不满足,整批拒收。
- 患者号 -> 稳定代号(HMAC,密钥持久化在数据卷,跨批/跨重启一致)。
- 检查/序列/实例 UID 全部重编;按"值"递归替换,序列内嵌套引用同步改写;
  SOP Class UID 与 Transfer Syntax UID 不在映射表内,天然保持不变;像素数据不触碰。
- 完整包落盘 + 映射登记在同一先后序列里都成功,批次才置为 COMPLETED 供下载。
- 失败时仅清除本批独占的 uid_map 记录,患者代号等全局映射不动,不影响其他成功批。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset

from . import db
from .config import (
    CT_IMAGE_STORAGE,
    EXPLICIT_VR_LITTLE_ENDIAN,
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


def _fail(batch_id: str, status: str, reason: str) -> None:
    """失败/拒收清理:删本批独占的 UID 映射、删半成品包;全局患者映射不动。"""
    log.warning("批次 %s %s: %s", batch_id, status, reason)
    with db.tx() as c:
        c.execute("DELETE FROM uid_map WHERE batch_id = ?", (batch_id,))
        c.execute(
            "UPDATE batches SET status = ?, error = ?, updated_at = ? WHERE batch_id = ?",
            (status, reason, utcnow(), batch_id),
        )
    for p in (OUTPUT_DIR / f"{batch_id}.zip", OUTPUT_DIR / f"{batch_id}.zip.tmp"):
        p.unlink(missing_ok=True)


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

    except BatchRejected as e:
        _fail(batch_id, "REJECTED", str(e))
    except Exception as e:  # noqa: BLE001 - 任何意外都不能拖垮工作线程
        log.exception("批次 %s 处理异常", batch_id)
        _fail(batch_id, "FAILED", f"内部错误: {e}")
    finally:
        # 处理结束(无论成败)删除原包与临时目录
        upload_path.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------- 重启恢复

def recover_interrupted() -> None:
    """服务重启:完成包保留可下载;未完成批标记 FAILED 并清理其独占映射与半成品。"""
    rows = db.query("SELECT batch_id FROM batches WHERE status IN ('PENDING','PROCESSING')")
    for r in rows:
        bid = r["batch_id"]
        with db.tx() as c:
            c.execute("DELETE FROM uid_map WHERE batch_id = ?", (bid,))
            c.execute(
                "UPDATE batches SET status = 'FAILED', error = ?, updated_at = ? WHERE batch_id = ?",
                ("服务重启,批次中断,未发布", utcnow(), bid),
            )
        (OUTPUT_DIR / f"{bid}.zip").unlink(missing_ok=True)
        (OUTPUT_DIR / f"{bid}.zip.tmp").unlink(missing_ok=True)
        log.info("重启清理:批次 %s 标记为 FAILED", bid)
    # 启动时没有任何批次在途,上传区/临时区里的一律是残留
    for p in UPLOAD_DIR.glob("*"):
        p.unlink(missing_ok=True)
    for p in TMP_DIR.glob("*"):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
