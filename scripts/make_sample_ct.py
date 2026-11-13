#!/usr/bin/env python3
"""生成虚构的 CT 检查 ZIP,用于演示/测试去标识化服务。

- 显式小端(Explicit VR Little Endian)、未压缩、CT Image Storage
- 每层带嵌套实例引用:ReferencedImageSequence 引用前一层,
  ReferencedSeriesSequence 内嵌 ReferencedSOPSequence 列出全序列实例
- 含私有标签、BurnedInAnnotation=NO
- 可选构造会被整批拒收的样本:--external-ref / --burned-in / --no-patient-id

用法:
    python3 scripts/make_sample_ct.py --out sample_ct.zip
    python3 scripts/make_sample_ct.py --out sample_bad.zip --external-ref
"""
from __future__ import annotations

import argparse
import struct
import uuid
import zipfile
from pathlib import Path

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

ROWS = COLS = 64


def make_slice(study_uid, series_uid, frame_uid, sop_uid, instance_no,
               patient_id, prev_sop_uid=None, all_sop_uids=(),
               burned_in="NO", external_ref=False) -> Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = "2.25." + str(uuid.uuid4().int)

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.FrameOfReferenceUID = frame_uid
    ds.Modality = "CT"
    ds.InstanceNumber = instance_no
    ds.SliceLocation = float(instance_no) * 2.5

    # 虚构患者信息(纯属演示,非真实数据)
    ds.PatientName = "Demo^Ct^Patient"
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19750312"
    ds.PatientSex = "O"
    ds.AccessionNumber = "ACC-DEMO-0001"
    ds.StudyDate = "20260101"
    ds.StudyTime = "120000"
    ds.StudyDescription = "Fictional CT demo"
    ds.SeriesDescription = "Demo axial slices"
    ds.BurnedInAnnotation = burned_in

    # 私有标签(演示删除)
    ds.add_new((0x0011, 0x0010), "LO", "DEMO PRIVATE CREATOR")
    ds.add_new((0x0011, 0x1001), "LO", "private-note-to-be-removed")

    # 图像像素(确定性渐变,便于校验像素不变)
    ds.Rows = ROWS
    ds.Columns = COLS
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.RescaleIntercept = "-1024.0"
    ds.RescaleSlope = "1.0"
    px = b"".join(
        struct.pack("<h", ((r * COLS + c + instance_no * 7) % 2048) - 1024)
        for r in range(ROWS) for c in range(COLS)
    )
    ds.PixelData = px

    # --- 嵌套实例引用 ---
    # 1) 引用前一层(ReferencedImageSequence)
    if prev_sop_uid:
        ref = Dataset()
        ref.ReferencedSOPClassUID = CTImageStorage
        ref.ReferencedSOPInstanceUID = prev_sop_uid
        ds.ReferencedImageSequence = Sequence([ref])

    # 2) 序列级引用,内嵌 ReferencedSOPSequence 列出本序列全部实例
    series_ref = Dataset()
    series_ref.SeriesInstanceUID = series_uid
    series_ref.ReferencedSOPSequence = Sequence([
        (lambda d: (setattr(d, "ReferencedSOPClassUID", CTImageStorage),
                    setattr(d, "ReferencedSOPInstanceUID", u), d)[-1])(Dataset())
        for u in all_sop_uids
    ])
    ds.ReferencedSeriesSequence = Sequence([series_ref])

    # 3) 检查级引用
    study_ref = Dataset()
    study_ref.StudyInstanceUID = study_uid
    ds.ReferencedStudySequence = Sequence([study_ref])

    # 4) 可选:故意引用包外实例(触发整批拒收)
    if external_ref:
        bad = Dataset()
        bad.ReferencedSOPClassUID = CTImageStorage
        bad.ReferencedSOPInstanceUID = "2.25.99999999999999999999999999999999999999"
        ds.SourceImageSequence = Sequence([bad])

    return ds


def main() -> None:
    ap = argparse.ArgumentParser(description="生成虚构 CT ZIP 样本")
    ap.add_argument("--out", default="sample_ct.zip", help="输出 ZIP 路径")
    ap.add_argument("--slices", type=int, default=3, help="层数")
    ap.add_argument("--patient-id", default="DEMO-PAT-001")
    ap.add_argument("--external-ref", action="store_true", help="加入包外实例引用(应被拒收)")
    ap.add_argument("--burned-in", action="store_true", help="BurnedInAnnotation=YES(应被拒收)")
    ap.add_argument("--no-patient-id", action="store_true", help="清空 PatientID(应被拒收)")
    args = ap.parse_args()

    study_uid = generate_uid()
    series_uid = generate_uid()
    frame_uid = generate_uid()
    sop_uids = [generate_uid() for _ in range(args.slices)]
    patient_id = "" if args.no_patient_id else args.patient_id

    out = Path(args.out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, sop in enumerate(sop_uids, start=1):
            ds = make_slice(
                study_uid, series_uid, frame_uid, sop, i, patient_id,
                prev_sop_uid=sop_uids[i - 2] if i > 1 else None,
                all_sop_uids=sop_uids,
                burned_in="YES" if args.burned_in else "NO",
                external_ref=args.external_ref and i == args.slices,
            )
            tmp = out.parent / f".slice_{i}.dcm"
            ds.save_as(str(tmp), write_like_original=False)
            zf.write(tmp, arcname=f"slice_{i:03d}.dcm")
            tmp.unlink()
    print(f"已生成 {out}({args.slices} 层,study={study_uid})")


if __name__ == "__main__":
    main()
