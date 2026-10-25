"""Shared benchmark release fixtures; synthetic evidence only."""

import csv
import io
from zipfile import ZipFile

from ashare_data.storage.cmes_release import import_cmes

from tests.support.daily_release import daily_jobs as daily_jobs
from tests.support.daily_release import fixed_release as fixed_release


def reference(tmp_path, days, missing):
    original = tmp_path / "index.zip"
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["时间", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"])
    writer.writerows([day, 100 + i, 100 + i, 100 + i, 100 + i, "", ""]
                     for i, day in enumerate(days) if day not in missing)
    with ZipFile(original, "w") as archive:
        archive.writestr("SH.000300.csv", stream.getvalue())
    root = tmp_path / "benchmark-release"
    receipt = import_cmes(root, daily=[original], daily_profile="daily-index-points", free_reserve_bytes=0)
    pin = dict(root=str(root), release_id=receipt["release_id"], manifest_sha256=receipt["manifest_sha256"])
    return pin
