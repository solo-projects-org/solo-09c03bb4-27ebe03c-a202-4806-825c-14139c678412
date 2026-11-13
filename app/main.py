"""FastAPI 入口:上传 CT ZIP、查询批次、下载去标识化包。Swagger 见 /docs。"""
from __future__ import annotations

import logging
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import db, processing
from .config import OUTPUT_DIR, UPLOAD_DIR, WORKERS, ensure_dirs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="deid")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    db.init()
    processing.recover_interrupted()  # 重启恢复:完成包保留,未完成批清理
    yield
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="CT 影像去标识化服务",
    description=(
        "上传显式小端非压缩 CT 的 ZIP 包,服务异步完成去标识化:"
        "清空姓名/出生日期/检查号、删除私有标签、患者号换稳定代号、"
        "检查/序列/实例 UID 重编并同步改写文件元信息与嵌套实例引用,"
        "SOP Class 与传输语法 UID 及像素数据保持不变。"
        "缺患者号、存在包外实例引用或 BurnedInAnnotation 非 NO 时整批拒收。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 模型

def batch_view(row) -> dict:
    view = {
        "batch_id": row["batch_id"],
        "status": row["status"],
        "original_filename": row["original_filename"],
        "file_count": row["file_count"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }
    if row["status"] == "COMPLETED":
        view["download_url"] = f"/api/batches/{row['batch_id']}/download"
    return view


def get_batch_or_404(batch_id: str):
    row = db.query_one("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return row


# ---------------------------------------------------------------- 路由

@app.get("/api/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/api/batches", status_code=202, tags=["batches"],
          summary="上传 CT ZIP 并创建去标识化批次")
async def create_batch(file: UploadFile = File(..., description="显式小端非压缩 CT 的 ZIP 包")):
    batch_id = uuid.uuid4().hex
    upload_path = UPLOAD_DIR / f"{batch_id}.zip"
    try:
        with open(upload_path, "wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        await file.close()

    now = processing.utcnow()
    with db.tx() as c:
        c.execute(
            "INSERT INTO batches(batch_id, status, original_filename, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (batch_id, "PENDING", file.filename, now, now),
        )
    executor.submit(processing.process_batch, batch_id)
    return {
        "batch_id": batch_id,
        "status": "PENDING",
        "status_url": f"/api/batches/{batch_id}",
    }


@app.get("/api/batches", tags=["batches"], summary="列出全部批次")
def list_batches():
    rows = db.query("SELECT * FROM batches ORDER BY created_at DESC")
    return [batch_view(r) for r in rows]


@app.get("/api/batches/{batch_id}", tags=["batches"], summary="查询批次状态")
def get_batch(batch_id: str):
    return batch_view(get_batch_or_404(batch_id))


@app.get("/api/batches/{batch_id}/download", tags=["batches"],
         summary="下载去标识化后的 ZIP 包")
def download_batch(batch_id: str):
    row = get_batch_or_404(batch_id)
    if row["status"] != "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail=f"批次状态为 {row['status']},不可下载"
                   + (f":{row['error']}" if row["error"] else ""),
        )
    path = OUTPUT_DIR / f"{batch_id}.zip"
    if not path.exists():
        raise HTTPException(status_code=410, detail="结果包文件缺失")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"deidentified_{batch_id}.zip",
    )
