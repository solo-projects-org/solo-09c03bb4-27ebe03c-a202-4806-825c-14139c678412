#!/usr/bin/env python3
"""本地演示:残件删除权限故障 + 清理中途退出后的自动补清。

脚本在独立临时目录上用 uvicorn 启动真实服务(与 Docker 内同一应用),
通过 DATA_DIR/faults/ 下的标记文件模拟"删除权限被拒绝"(等价于卷权限错误):
  - delete_file 标记存在时,文件删除抛 PermissionError
  - rmtree      标记存在时,目录删除抛 PermissionError

演示两个场景:
  场景 A 权限故障:
    1) 置故障标记,上传一个会被拒收的批次 -> 批次照常 REJECTED,服务正常可查
    2) 清理进度卡在 FAILED,残件与 SQLite 中的清理依据都保留
    3) 同时上传正常批次 -> 处理/下载不受阻塞(慢清理不阻塞其他批次)
    4) 解除故障(模拟运维恢复卷权限) -> 无须重启,后台自动补清到 DONE
  场景 B 清理中途退出:
    1) 故障状态下再制造一个残件未清的失败批
    2) 直接杀掉服务进程(模拟清理中途退出/容器重启)
    3) 重新启动服务 -> 依据仍在,启动后自动续清;成功批的结果与映射保留

用法:
  python3 scripts/demo_cleanup_recovery.py
环境变量:
  SKIP_BUILD=1   跳过依赖检查(假定 fastapi/uvicorn/pydicom 已安装)
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "sample_data"
PYTHON = sys.executable


def log(step: str, msg: str) -> None:
    print(f"\n\033[1;36m【{step}】\033[0m {msg}")


def wait_port(port: int, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"服务端口 {port} 未在 {timeout}s 内就绪")


def http(method: str, url: str, data: bytes | None = None,
         content_type: str | None = None, raw: bool = False) -> tuple[int, object]:
    req = urllib.request.Request(url, data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
            if raw:
                return r.status, payload
            return r.status, json.loads(payload.decode() or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, payload
        return e.code, json.loads(payload.decode() or "{}")


def multipart_body(path: Path) -> tuple[bytes, str]:
    boundary = "----deid-demo-" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/zip\r\n\r\n",
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def upload(base: str, sample: str) -> str:
    body, ctype = multipart_body(SAMPLES / sample)
    code, data = http("POST", f"{base}/api/batches", body, ctype)
    assert code == 202, (code, data)
    return data["batch_id"]


def wait_status(base: str, bid: str, wanted: set[str], timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = http("GET", f"{base}/api/batches/{bid}")
        if data["status"] in wanted:
            return data
        time.sleep(0.2)
    raise TimeoutError(bid)


def wait_cleanup(base: str, bid: str, wanted: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    data = {}
    while time.time() < deadline:
        _, data = http("GET", f"{base}/api/batches/{bid}")
        if data["cleanup_status"] == wanted:
            return data
        time.sleep(0.2)
    raise AssertionError(f"{bid} 清理状态未到 {wanted}: {data}")


class Server:
    def __init__(self, data_dir: Path, port: int):
        self.data_dir = data_dir
        self.port = port
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        # cwd 固定为项目根(与临时数据目录分离,不会从样本目录加载任何模块)
        import site
        extra = [str(ROOT)]
        user_site = site.getusersitepackages()
        if Path(user_site).exists():
            extra.append(user_site)
        env = {
            **os.environ,
            "DATA_DIR": str(self.data_dir),
            "DEID_CLEANUP_INTERVAL": "1",
            "DEID_FAULT_INJECTION": "1",
            "PYTHONPATH": os.pathsep.join(extra),
            "PYTHONNOUSERSITE": "0",
        }
        logdir = self.data_dir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        self.log = (logdir / "uvicorn.log").open("a")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(ROOT), env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        wait_port(self.port)

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log:
            self.log.close()


def main() -> None:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        import pydicom  # noqa: F401
    except ImportError:
        print("缺少依赖,请先安装: python3 -m pip install -r requirements.txt")
        sys.exit(1)

    work = Path(tempfile.mkdtemp(prefix="deid-demo-"))
    data_dir = work / "data"
    port = 8765
    base = f"http://127.0.0.1:{port}"
    print(f"演示数据目录: {data_dir}")

    server = Server(data_dir, port)
    try:
        # ================================================== 场景 A:权限故障
        log("A1", "启动服务(故障注入已开启)")
        server.start()
        print("  Swagger: 暂未开放外部访问,接口基址 =", base)

        log("A2", "制造删除权限故障:置 delete_file / rmtree 标记")
        faults = data_dir / "faults"
        faults.mkdir(parents=True, exist_ok=True)
        (faults / "delete_file").write_text("on")
        (faults / "rmtree").write_text("on")

        log("A3", "上传会被拒收的批次(BurnedInAnnotation=YES)")
        bad = upload(base, "sample_burned_in.zip")
        info = wait_status(base, bad, {"REJECTED", "FAILED"})
        print(f"  批次 {bad[:8]} 处理结局 = {info['status']}: {info['error']}")
        assert info["status"] == "REJECTED"

        log("A4", "等待后台补清重试,清理进度应停在 FAILED 且残件保留")
        time.sleep(2.5)
        _, info = http("GET", f"{base}/api/batches/{bad}")
        print(f"  cleanup_status={info['cleanup_status']} "
              f"remaining={info['cleanup_remaining']} error={info['cleanup_error']}")
        assert info["cleanup_status"] == "FAILED"
        leftover = data_dir / "uploads" / f"{bad}.zip"
        assert leftover.exists(), "权限未恢复时残件必须仍在"
        print(f"  残件仍在: {leftover.relative_to(data_dir)}")
        print("  服务健康检查 =", http("GET", f"{base}/api/health")[1])

        log("A5", "故障期间上传正常批次,验证不被慢清理阻塞")
        good = upload(base, "sample_ct.zip")
        info = wait_status(base, good, {"COMPLETED"})
        print(f"  正常批次 {good[:8]} = {info['status']}, file_count={info['file_count']}")
        assert info["status"] == "COMPLETED"
        code, pkg = http("GET", f"{base}/api/batches/{good}/download", raw=True)
        print(f"  正常批次下载 HTTP {code}(200 表示发布不受清理故障影响), 包大小 {len(pkg)} 字节")
        assert code == 200

        log("A6", "解除权限故障(模拟运维恢复卷权限),不重启服务")
        (faults / "delete_file").unlink()
        (faults / "rmtree").unlink()
        info = wait_cleanup(base, bad, "DONE")
        print(f"  失败批清理完成: cleanup_status={info['cleanup_status']} "
              f"total={info['cleanup_total']} remaining={info['cleanup_remaining']}")
        wait_cleanup(base, good, "DONE")
        assert not leftover.exists()
        assert (data_dir / "output" / f"{good}.zip").exists(), "成功批结果包必须保留"
        print("  失败批残件已自动补清;成功批结果包与映射保留,全程未重启服务 ✅")

        # ================================================== 场景 B:中途退出
        log("B1", "再次制造权限故障并上传拒收批,然后杀掉服务(模拟清理中途退出)")
        (faults / "delete_file").write_text("on")
        (faults / "rmtree").write_text("on")
        bad2 = upload(base, "sample_external_ref.zip")
        wait_status(base, bad2, {"REJECTED", "FAILED"})
        time.sleep(2.0)
        _, before = http("GET", f"{base}/api/batches/{bad2}")
        print(f"  退出前 cleanup_status={before['cleanup_status']} "
              f"remaining={before['cleanup_remaining']}")
        assert (data_dir / "uploads" / f"{bad2}.zip").exists()
        server.stop()
        print("  服务进程已终止(SIGTERM),清理依据保留在 SQLite 中")

        log("B2", "解除故障并重启服务,验证启动后自动续清,无须人工干预")
        (faults / "delete_file").unlink()
        (faults / "rmtree").unlink()
        server.start()
        info = wait_cleanup(base, bad2, "DONE")
        print(f"  重启后批次 {bad2[:8]} 自动补清完成: {info['cleanup_status']}")
        assert not (data_dir / "uploads" / f"{bad2}.zip").exists()
        assert not (data_dir / "tmp" / bad2).exists()

        log("B3", "校验跨重启的成功批:结局、结果包、下载入口、独占映射均保留")
        _, info = http("GET", f"{base}/api/batches/{good}")
        print(f"  批次 {good[:8]} status={info['status']} "
              f"cleanup={info['cleanup_status']} download_url={info.get('download_url')}")
        assert info["status"] == "COMPLETED"
        code, pkg = http("GET", f"{base}/api/batches/{good}/download", raw=True)
        assert code == 200 and len(pkg) > 0

        import sqlite3
        con = sqlite3.connect(data_dir / "deid.db")
        n_maps = con.execute(
            "SELECT COUNT(*) FROM uid_map WHERE batch_id=?", (good,)).fetchone()[0]
        n_left = con.execute(
            "SELECT COUNT(*) FROM cleanup_targets WHERE status!='DONE'").fetchone()[0]
        con.close()
        print(f"  成功批独占 UID 映射保留 {n_maps} 条;全库未完成清理依据剩余 {n_left} 条")
        assert n_maps == 5 and n_left == 0

        print("\n\033[1;32m两个场景全部演示通过 ✅\033[0m")
        print(f"日志与数据保留在: {data_dir}(可自行查阅 deid.db / logs/uvicorn.log)")
    finally:
        server.stop()
        if "--keep" not in sys.argv:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
