"""pytest 公共夹具:每个测试使用独立的临时 DATA_DIR,并通过 TestClient 走完整生命周期。

注意:app.config 在导入时读取环境变量,因此必须在导入任何 app.* 模块之前设置 DATA_DIR。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = tempfile.mkdtemp(prefix="deid-test-")
os.environ["DATA_DIR"] = str(Path(_TMP_ROOT) / "data")
os.environ["DEID_CLEANUP_INTERVAL"] = "0.05"          # 测试里快速补清
os.environ["DEID_FAULT_INJECTION"] = "1"             # 打开故障注入能力

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, processing  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture()
def client(monkeypatch):
    """全新数据目录 + 完整 lifespan(含对账与补清线程)的测试客户端。"""
    data_dir = Path(tempfile.mkdtemp(dir=_TMP_ROOT))
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "UPLOAD_DIR", data_dir / "uploads")
    monkeypatch.setattr(config, "OUTPUT_DIR", data_dir / "output")
    monkeypatch.setattr(config, "TMP_DIR", data_dir / "tmp")
    monkeypatch.setattr(config, "DB_PATH", data_dir / "deid.db")
    monkeypatch.setattr(config, "SECRET_PATH", data_dir / "secret.key")
    monkeypatch.setattr(config, "FAULT_DIR", data_dir / "faults")
    monkeypatch.setattr(processing, "UPLOAD_DIR", config.UPLOAD_DIR)
    monkeypatch.setattr(processing, "OUTPUT_DIR", config.OUTPUT_DIR)
    monkeypatch.setattr(processing, "TMP_DIR", config.TMP_DIR)
    monkeypatch.setattr(processing, "FAULT_DIR", config.FAULT_DIR)
    monkeypatch.setattr(processing, "SECRET_PATH", config.SECRET_PATH)
    monkeypatch.setattr(processing, "_secret", None)  # 重置 HMAC 密钥缓存
    monkeypatch.setattr(db, "DB_PATH", config.DB_PATH)

    config.ensure_dirs()
    # 重置 sweeper 单例状态,保证每个用例独立
    processing.sweeper.stop()

    from app.main import app
    with TestClient(app) as c:
        yield c

    processing.sweeper.stop()
    db.close()


def upload(client: TestClient, sample: str) -> str:
    r = client.post(
        "/api/batches",
        files={"file": (sample, (SAMPLES / sample).read_bytes(), "application/zip")},
    )
    assert r.status_code == 202, r.text
    return r.json()["batch_id"]


def wait_terminal(client: TestClient, batch_id: str, timeout: float = 30) -> dict:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/batches/{batch_id}").json()
        if body["status"] in ("COMPLETED", "REJECTED", "FAILED"):
            return body
        time.sleep(0.05)
    raise TimeoutError(batch_id)


def wait_cleanup(client: TestClient, batch_id: str, timeout: float = 15) -> dict:
    import time
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = client.get(f"/api/batches/{batch_id}").json()
        if body["cleanup_status"] == "DONE":
            return body
        time.sleep(0.05)
    raise AssertionError(f"清理未完成: {body}")
