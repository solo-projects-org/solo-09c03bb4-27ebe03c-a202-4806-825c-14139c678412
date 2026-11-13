"""路径与常量配置。所有持久化内容都落在 DATA_DIR(数据卷)里。"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

UPLOAD_DIR = DATA_DIR / "uploads"   # 待处理的原始上传包(处理完即删)
OUTPUT_DIR = DATA_DIR / "output"    # 已完成、可下载的去标识化包
TMP_DIR = DATA_DIR / "tmp"          # 处理中的临时解包/改写目录
DB_PATH = DATA_DIR / "deid.db"      # SQLite 映射库
SECRET_PATH = DATA_DIR / "secret.key"  # 患者代号 HMAC 密钥,持久化保证跨重启稳定

# 并行处理的工作线程数:两批可并行,一批慢打包不阻塞另一批
WORKERS = int(os.environ.get("DEID_WORKERS", "2"))

CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
EXPLICIT_VR_LITTLE_ENDIAN = "1.2.840.10008.1.2.1"


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
