#!/usr/bin/env python3
"""端到端冒烟测试:上传 -> 轮询 -> 下载 -> 逐项校验去标识化结果。"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pydicom

BASE = "http://127.0.0.1:8000"
SAMPLES = Path("sample_data")
FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILURES.append(msg)


def upload(path: Path) -> str:
    with httpx.Client(base_url=BASE, timeout=30) as c:
        r = c.post("/api/batches", files={"file": (path.name, path.read_bytes(), "application/zip")})
        r.raise_for_status()
        return r.json()["batch_id"]


def wait_done(batch_id: str, timeout: float = 60) -> dict:
    with httpx.Client(base_url=BASE, timeout=30) as c:
        for _ in range(int(timeout * 10)):
            r = c.get(f"/api/batches/{batch_id}")
            r.raise_for_status()
            body = r.json()
            if body["status"] in ("COMPLETED", "REJECTED", "FAILED"):
                return body
            time.sleep(0.1)
    raise TimeoutError(f"批次 {batch_id} 超时未完成")


def download(batch_id: str) -> bytes:
    with httpx.Client(base_url=BASE, timeout=30) as c:
        r = c.get(f"/api/batches/{batch_id}/download")
        r.raise_for_status()
        return r.content


def read_zip_datasets(zip_bytes: bytes) -> dict[str, pydicom.Dataset]:
    out = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            out[name] = pydicom.dcmread(io.BytesIO(zf.read(name)))
    return out


def collect_refs(ds) -> set[str]:
    refs = set()
    def walk(d, top=False):
        for elem in d:
            if elem.VR == "SQ":
                for item in elem.value:
                    walk(item)
            elif elem.VR == "UI" and "SOPInstanceUID" in elem.keyword:
                if top and elem.keyword == "SOPInstanceUID":
                    continue
                refs.add(str(elem.value))
    walk(ds, top=True)
    return refs


def main() -> None:
    # ---------- 原始样本信息 ----------
    orig = read_zip_datasets(SAMPLES.joinpath("sample_ct.zip").read_bytes())
    orig_sops = {str(ds.SOPInstanceUID) for ds in orig.values()}
    orig_study = str(next(iter(orig.values())).StudyInstanceUID)
    orig_series = str(next(iter(orig.values())).SeriesInstanceUID)
    orig_pixels = {str(ds.SOPInstanceUID): ds.PixelData for ds in orig.values()}

    print("== 1. 正常批次:上传 -> 完成 -> 下载 ==")
    b1 = upload(SAMPLES / "sample_ct.zip")
    st = wait_done(b1)
    check(st["status"] == "COMPLETED", f"批次完成(实际 {st['status']} {st.get('error')})")
    check(st["file_count"] == 3, "file_count=3")
    pkg = download(b1)
    new = read_zip_datasets(pkg)

    print("== 2. 去标识化内容校验 ==")
    check(len(new) == 3, "包内 3 个文件")
    new_sops = {str(ds.SOPInstanceUID) for ds in new.values()}
    check(not (new_sops & orig_sops), "实例 UID 全部重编,与原 UID 无交集")
    check(all(name == f"{ds.SOPInstanceUID}.dcm" for name, ds in new.items()),
          "文件名 == 新实例 UID.dcm")
    check(all(str(ds.PatientName) == "" for ds in new.values()), "姓名已清空")
    check(all(str(ds.PatientBirthDate) == "" for ds in new.values()), "出生日期已清空")
    check(all(str(ds.AccessionNumber) == "" for ds in new.values()), "检查号已清空")
    check(all(not any(t.is_private for t in ds.keys()) for ds in new.values()), "私有标签已删除")
    pids = {str(ds.PatientID) for ds in new.values()}
    check(len(pids) == 1 and next(iter(pids)).startswith("PT-"), f"患者号换为稳定代号 {pids}")
    check(next(iter(pids)) != "DEMO-PAT-001", "代号不含原患者号")
    check(all(str(ds.SOPClassUID) == "1.2.840.10008.5.1.4.1.1.2" for ds in new.values()),
          "SOP Class UID 不变")
    check(all(str(ds.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.1" for ds in new.values()),
          "传输语法 UID 不变(显式小端)")
    check(all(str(ds.file_meta.MediaStorageSOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"
              for ds in new.values()), "文件元信息 SOP Class 不变")
    check(all(str(ds.file_meta.MediaStorageSOPInstanceUID) == str(ds.SOPInstanceUID)
              for ds in new.values()), "文件元信息实例 UID 与数据集同步")
    new_study = {str(ds.StudyInstanceUID) for ds in new.values()}
    new_series = {str(ds.SeriesInstanceUID) for ds in new.values()}
    check(len(new_study) == 1 and orig_study not in new_study, "检查 UID 重编且一致")
    check(len(new_series) == 1 and orig_series not in new_series, "序列 UID 重编且一致")

    print("== 3. 嵌套引用与像素 ==")
    for name, ds in new.items():
        refs = collect_refs(ds)
        check(refs <= new_sops, f"{name}: 嵌套实例引用全部指向包内新 UID")
    # 像素:新旧包按"层"对应(原引用关系:第 i 层引用第 i-1 层)
    def order_by_ref(dsets):
        sops = {str(d.SOPInstanceUID): d for d in dsets.values()}
        # 链头 = 自身不引用任何层的切片
        first = next(s for s, d in sops.items()
                     if not d.get("ReferencedImageSequence"))
        ordered, cur = [], first
        while cur:
            ordered.append(cur)
            nxt = [s for s, d in sops.items()
                   for e in d.get("ReferencedImageSequence", [])
                   if str(e.ReferencedSOPInstanceUID) == cur]
            cur = nxt[0] if nxt else None
        return ordered
    orig_order = order_by_ref(orig)
    new_order = order_by_ref(new)
    check(len(new_order) == 3, "新包引用链完整(3 层)")
    for o_sop, n_sop in zip(orig_order, new_order):
        check(orig_pixels[o_sop] == sops_pixels(new, n_sop), f"像素不变(层 {o_sop[:12]}...)")
    # 原标识对照不得出现在包内
    raw = pkg
    check(orig_study.encode() not in raw and orig_series.encode() not in raw
          and all(s.encode() not in raw for s in orig_sops)
          and b"DEMO-PAT-001" not in raw and b"Demo" not in raw,
          "包内不含任何原标识/原 UID 对照")

    print("== 4. 患者代号跨批稳定 ==")
    b2 = upload(SAMPLES / "sample_ct.zip")
    st2 = wait_done(b2)
    check(st2["status"] == "COMPLETED", "第二批完成")
    new2 = read_zip_datasets(download(b2))
    pids2 = {str(ds.PatientID) for ds in new2.values()}
    check(pids2 == pids, f"同一患者代号一致 {pids2}")
    sops2 = {str(ds.SOPInstanceUID) for ds in new2.values()}
    check(not (sops2 & new_sops), "不同批次实例 UID 各自重编(批独占)")

    print("== 5. 整批拒收场景 ==")
    for fname, kw in [("sample_external_ref.zip", "包外"),
                      ("sample_burned_in.zip", "BurnedInAnnotation"),
                      ("sample_no_patientid.zip", "PatientID")]:
        b = upload(SAMPLES / fname)
        st = wait_done(b)
        check(st["status"] == "REJECTED" and kw in (st["error"] or ""),
              f"{fname} 被拒收({st['status']}: {st['error']})")
        with httpx.Client(base_url=BASE, timeout=10) as c:
            r = c.get(f"/api/batches/{b}/download")
        check(r.status_code == 409, f"{fname} 拒收批不可下载(HTTP {r.status_code})")

    print("== 6. 并发:两批并行 ==")
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(upload, SAMPLES / "sample_ct.zip")
        f2 = ex.submit(upload, SAMPLES / "sample_ct.zip")
        ba, bb = f1.result(), f2.result()
    sa, sb = wait_done(ba), wait_done(bb)
    check(sa["status"] == "COMPLETED" and sb["status"] == "COMPLETED", "两并发批均完成")

    print("== 7. SQLite 映射库一致性 ==")
    db_path = Path(os.environ.get("DEID_DB", "/tmp/deid-data/deid.db"))
    con = sqlite3.connect(db_path)
    n_maps = con.execute("SELECT COUNT(*) FROM uid_map WHERE batch_id=?", (b1,)).fetchone()[0]
    check(n_maps == 5, f"批次1 映射 5 条(1 检查+1 序列+3 实例),实际 {n_maps}")
    rej = con.execute(
        "SELECT COUNT(*) FROM uid_map u JOIN batches b ON u.batch_id=b.batch_id"
        " WHERE b.status IN ('REJECTED','FAILED')").fetchone()[0]
    check(rej == 0, "失败/拒收批的独占映射已清除")
    n_pat = con.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    check(n_pat == 1, f"患者映射仅 1 条且跨批复用(实际 {n_pat})")
    con.close()

    print("== 8. 原包已删除 ==")
    up = Path(os.environ.get("DEID_UPLOADS", "/tmp/deid-data/uploads"))
    check(not any(up.iterdir()), "上传区已清空(原包处理完即删)")

    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败:")
        for f in FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✅")


def sops_pixels(dsets, sop):
    for d in dsets.values():
        if str(d.SOPInstanceUID) == sop:
            return d.PixelData
    raise KeyError(sop)


if __name__ == "__main__":
    main()
