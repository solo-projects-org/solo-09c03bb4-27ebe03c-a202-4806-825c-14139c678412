"""批次清理恢复专项测试。

覆盖:
1. 成功批:结果与映射保留,原包/临时目录清完,清理进度 DONE
2. 拒收/失败批:独占映射与全部残件清除,未发布包不可下载
3. 权限故障:删除失败不丢依据,API 与其他批次不被阻塞;故障解除后自动补清(免重启)
4. 清理中途退出(重启):依据持久化,重启后续清;中断批标 FAILED
5. 旧数据卷兼容:已标 FAILED 但仍有残件的批次,启动后自动补登记并清完
6. 后台补清白名单:绝不删除新上传批次的在途文件
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app import config, db, processing
from tests.conftest import SAMPLES, upload, wait_cleanup, wait_terminal


def _fault_on(name: str) -> None:
    config.FAULT_DIR.mkdir(parents=True, exist_ok=True)
    (config.FAULT_DIR / name).write_text("on")


def _fault_off(name: str) -> None:
    p = config.FAULT_DIR / name
    if p.exists():
        p.unlink()


def _batch_paths(bid: str):
    return {
        "upload": config.UPLOAD_DIR / f"{bid}.zip",
        "workdir": config.TMP_DIR / bid,
        "output": config.OUTPUT_DIR / f"{bid}.zip",
        "tmp_zip": config.OUTPUT_DIR / f"{bid}.zip.tmp",
    }


def test_completed_batch_retains_result_and_maps(client):
    bid = upload(client, "sample_ct.zip")
    body = wait_terminal(client, bid)
    assert body["status"] == "COMPLETED"

    body = wait_cleanup(client, bid)
    paths = _batch_paths(bid)
    # 成功批:结果包保留、可下载;原包与临时目录已清
    assert paths["output"].exists()
    assert not paths["upload"].exists()
    assert not paths["workdir"].exists()
    assert body["cleanup_status"] == "DONE"
    assert body["cleanup_remaining"] == 0
    assert body["cleanup_total"] == 2  # 成功批只登记原包 + 临时目录

    r = client.get(f"/api/batches/{bid}/download")
    assert r.status_code == 200

    con = sqlite3.connect(config.DB_PATH)
    n_maps = con.execute("SELECT COUNT(*) FROM uid_map WHERE batch_id=?", (bid,)).fetchone()[0]
    assert n_maps == 5  # 1 检查 + 1 序列 + 3 实例
    con.close()


def test_rejected_batch_fully_cleaned_and_not_downloadable(client):
    bid = upload(client, "sample_burned_in.zip")
    body = wait_terminal(client, bid)
    assert body["status"] == "REJECTED"

    body = wait_cleanup(client, bid)
    assert body["cleanup_status"] == "DONE"
    for p in _batch_paths(bid).values():
        assert not p.exists()

    r = client.get(f"/api/batches/{bid}/download")
    assert r.status_code == 409

    con = sqlite3.connect(config.DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM uid_map WHERE batch_id=?", (bid,)).fetchone()[0]
    assert n == 0
    con.close()


def test_permission_failure_then_auto_recovery_without_restart(client):
    # 让所有删除动作都被"权限拒绝"
    _fault_on("delete_file")
    _fault_on("rmtree")

    bid = upload(client, "sample_burned_in.zip")
    body = wait_terminal(client, bid)
    assert body["status"] == "REJECTED"  # 处理结局照常落库,服务不受删除故障影响

    # 等待补清线程至少重试几轮,清理进度应停在 FAILED 且依据保留
    deadline = time.time() + 3
    saw_failed = False
    while time.time() < deadline:
        body = client.get(f"/api/batches/{bid}").json()
        if body["cleanup_status"] == "FAILED":
            saw_failed = True
            break
        time.sleep(0.05)
    assert saw_failed
    paths = _batch_paths(bid)
    assert paths["upload"].exists()  # 残件仍在
    assert body["cleanup_remaining"] >= 1
    assert body["cleanup_error"] and "Permission" in body["cleanup_error"]

    # 依据仍在 SQLite,未被丢弃
    con = sqlite3.connect(config.DB_PATH)
    pending = con.execute(
        "SELECT COUNT(*) FROM cleanup_targets WHERE batch_id=? AND status!='DONE'", (bid,)
    ).fetchone()[0]
    con.close()
    assert pending >= 1

    # 故障期间,另一个正常批次(删除仍失败)的处理与查询不受阻塞
    other = upload(client, "sample_ct.zip")
    obody = wait_terminal(client, other)
    assert obody["status"] == "COMPLETED"
    # 结果包在位即可下载(它的原包清理失败不影响发布语义)
    assert _batch_paths(other)["output"].exists()
    assert client.get(f"/api/batches/{other}/download").status_code == 200

    # 解除权限故障:无须重启,后台补清自动清完两批残件
    _fault_off("delete_file")
    _fault_off("rmtree")
    wait_cleanup(client, bid)
    wait_cleanup(client, other)
    for b in (bid, other):
        for key, p in _batch_paths(b).items():
            if key == "output":
                continue  # 成功批结果包保留
            assert not p.exists(), (b, key)
    # 成功批的结果与映射仍在
    assert _batch_paths(other)["output"].exists()


def test_restart_mid_cleanup_resumes_from_persisted_targets(client):
    # 先制造一个删除失败、残件与依据都在的拒收批
    _fault_on("delete_file")
    _fault_on("rmtree")
    bid = upload(client, "sample_burned_in.zip")
    wait_terminal(client, bid)
    time.sleep(0.3)  # 让补清线程记账为 FAILED
    assert _batch_paths(bid)["upload"].exists()

    # 模拟进程退出再启动:TestClient 退出会 stop sweeper;重新进入触发生命周期
    from app.main import app
    from fastapi.testclient import TestClient

    # 重启前先解除故障,验证重启后依据仍在并被续清
    _fault_off("delete_file")
    _fault_off("rmtree")

    with TestClient(app) as c2:
        body = wait_cleanup(c2, bid)
        assert body["cleanup_status"] == "DONE"
        for key, p in _batch_paths(bid).items():
            assert not p.exists(), key
        # 处理结局没有被重启改写
        assert c2.get(f"/api/batches/{bid}").json()["status"] == "REJECTED"


def test_interrupted_batch_marked_failed_and_cleaned_on_restart(client):
    # 直接构造一个"处理到一半进程被杀"的批次:状态 PROCESSING + 在途文件
    bid = "deadbeef" * 4
    now = processing.utcnow()
    with db.tx() as c:
        c.execute(
            "INSERT INTO batches(batch_id, status, original_filename, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (bid, "PROCESSING", "interrupted.zip", now, now),
        )
        c.execute(
            "INSERT INTO uid_map(batch_id, original_uid, new_uid, uid_type, created_at)"
            " VALUES (?,?,?,?,?)",
            (bid, "1.2.3.old", "1.2.3.new", "INSTANCE", now),
        )
    upload_p = config.UPLOAD_DIR / f"{bid}.zip"
    upload_p.write_bytes(b"partial-upload")
    workdir = config.TMP_DIR / bid / "in"
    workdir.mkdir(parents=True)
    (workdir / "half.dcm").write_bytes(b"partial")
    out_tmp = config.OUTPUT_DIR / f"{bid}.zip.tmp"
    out_tmp.write_bytes(b"partial-zip")

    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c2:
        info = c2.get(f"/api/batches/{bid}").json()
        assert info["status"] == "FAILED"
        assert "中断" in (info["error"] or "")
        info = wait_cleanup(c2, bid)
        assert info["cleanup_status"] == "DONE"

    for p in (upload_p, config.TMP_DIR / bid, out_tmp,
              config.OUTPUT_DIR / f"{bid}.zip"):
        assert not p.exists()
    con = sqlite3.connect(config.DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM uid_map WHERE batch_id=?", (bid,)).fetchone()[0]
    assert n == 0
    con.close()


def test_legacy_volume_failed_batch_with_leftovers_is_reconciled(tmp_path, monkeypatch):
    """旧版本数据卷:batches 里已标 FAILED(无 cleanup_* 表/列),残件仍在盘上。"""
    data_dir = tmp_path / "legacy"
    (data_dir / "uploads").mkdir(parents=True)
    (data_dir / "output").mkdir(parents=True)
    (data_dir / "tmp").mkdir(parents=True)

    # 用旧 schema 手工建库(模拟升级前的卷)
    con = sqlite3.connect(data_dir / "deid.db")
    con.executescript(
        """
        CREATE TABLE patients (original_patient_id TEXT PRIMARY KEY, codename TEXT NOT NULL,
                               created_at TEXT NOT NULL);
        CREATE TABLE uid_map (batch_id TEXT NOT NULL, original_uid TEXT NOT NULL,
                              new_uid TEXT NOT NULL, uid_type TEXT NOT NULL,
                              created_at TEXT NOT NULL, PRIMARY KEY (batch_id, original_uid));
        CREATE TABLE batches (batch_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                              original_filename TEXT, file_count INTEGER, error TEXT,
                              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                              completed_at TEXT);
        """
    )
    bid = "legacybatch"
    con.execute(
        "INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)",
        (bid, "FAILED", "old.zip", None, "旧版本失败", "t0", "t0", None),
    )
    con.execute(
        "INSERT INTO uid_map VALUES (?,?,?,?,?)",
        (bid, "1.2.old", "1.2.new", "INSTANCE", "t0"),
    )
    con.commit()
    con.close()

    # 残件(旧版 _fail 因权限错误没删掉)
    (data_dir / "uploads" / f"{bid}.zip").write_bytes(b"x")
    (data_dir / "tmp" / bid).mkdir()
    (data_dir / "tmp" / bid / "junk").write_bytes(b"x")
    (data_dir / "output" / f"{bid}.zip.tmp").write_bytes(b"x")

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
    monkeypatch.setattr(processing, "_secret", None)
    monkeypatch.setattr(db, "DB_PATH", config.DB_PATH)
    processing.sweeper.stop()
    db.close()

    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        info = wait_cleanup(c, bid)
        assert info["status"] == "FAILED"           # 处理结局保留
        assert info["cleanup_status"] == "DONE"     # 残件补清完成
    assert not (data_dir / "uploads" / f"{bid}.zip").exists()
    assert not (data_dir / "tmp" / bid).exists()
    assert not (data_dir / "output" / f"{bid}.zip.tmp").exists()

    con = sqlite3.connect(data_dir / "deid.db")
    assert con.execute("SELECT COUNT(*) FROM uid_map WHERE batch_id=?", (bid,)).fetchone()[0] == 0
    # 迁移列已补齐
    cols = {r[1] for r in con.execute("PRAGMA table_info(batches)")}
    assert {"cleanup_status", "cleanup_total", "cleanup_remaining", "cleaned_at"} <= cols
    con.close()
    processing.sweeper.stop()
    db.close()


def test_sweeper_never_deletes_inflight_files_of_new_batch(client):
    # 一个清理卡住的失败批
    _fault_on("delete_file")
    _fault_on("rmtree")
    bad = upload(client, "sample_burned_in.zip")
    wait_terminal(client, bad)
    time.sleep(0.3)

    # 新批次正常上传并处理;其在途/结果文件绝不能被补清线程误删
    good = upload(client, "sample_ct.zip")
    body = wait_terminal(client, good)
    assert body["status"] == "COMPLETED"
    good_output = _batch_paths(good)["output"]
    assert good_output.exists()

    # 手动再触发多轮补清(仍故障),新批文件保持不动
    for _ in range(3):
        processing.sweeper.sweep_once()
    assert good_output.exists()

    # 失败批的白名单里不可能出现新批的任何路径
    con = sqlite3.connect(config.DB_PATH)
    rows = con.execute("SELECT path FROM cleanup_targets WHERE batch_id=?", (bad,)).fetchall()
    con.close()
    for (p,) in rows:
        assert good not in p

    _fault_off("delete_file")
    _fault_off("rmtree")
    wait_cleanup(client, bad)
    assert good_output.exists()  # 补清完成后新批结果依然完好


def test_unpublished_package_never_downloadable(client):
    # 拒收批
    bid = upload(client, "sample_no_patientid.zip")
    wait_terminal(client, bid)
    assert client.get(f"/api/batches/{bid}/download").status_code == 409
    # 即使外部在 output 目录放了一个同名文件(未发布残件),状态非 COMPLETED 仍拒绝
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rogue = config.OUTPUT_DIR / f"{bid}.zip"
    rogue.write_bytes(b"should-never-be-served")
    r = client.get(f"/api/batches/{bid}/download")
    assert r.status_code == 409
    # 补清线程对账发现终态批盘上又出现未发布结果,自动重新登记并清掉
    deadline = time.time() + 10
    while rogue.exists() and time.time() < deadline:
        processing.sweeper.wake()
        time.sleep(0.05)
    assert not rogue.exists()
    info = client.get(f"/api/batches/{bid}").json()
    assert info["cleanup_status"] == "DONE" and info["cleanup_remaining"] == 0


def test_two_batches_parallel_still_works(client):
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(upload, client, "sample_ct.zip")
        f2 = ex.submit(upload, client, "sample_ct.zip")
        b1, b2 = f1.result(), f2.result()
    assert wait_terminal(client, b1)["status"] == "COMPLETED"
    assert wait_terminal(client, b2)["status"] == "COMPLETED"
    wait_cleanup(client, b1)
    wait_cleanup(client, b2)
    for b in (b1, b2):
        assert _batch_paths(b)["output"].exists()
        assert not _batch_paths(b)["upload"].exists()
