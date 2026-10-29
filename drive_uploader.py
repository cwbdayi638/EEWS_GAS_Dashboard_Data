#!/usr/bin/env python3
"""
drive_uploader.py — Google Drive 上傳工具模組

提供透過 Service Account 自動上傳檔案至 Google Drive 的核心功能。
支援 JSON 資料（字串）與本機檔案（Path）兩種上傳方式，
以及依 YYYY_MM 月份子資料夾自動歸檔。

前置需求
--------
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

.env 設定（在 GITHUB_USERNAME / GITHUB_TOKEN 之後追加）
-------------------------------------------------------
GDRIVE_SERVICE_ACCOUNT_JSON=/path/to/service_account.json
GDRIVE_FOLDER_ID=your_google_drive_folder_id

如何取得 GDRIVE_FOLDER_ID
--------------------------
1. 在 Google Drive 建立一個共用資料夾
2. 用瀏覽器打開該資料夾，網址列會顯示：
   https://drive.google.com/drive/folders/<FOLDER_ID>
3. 複製 <FOLDER_ID> 填入 .env

如何建立 Service Account
--------------------------
1. 前往 Google Cloud Console > IAM & Admin > Service Accounts
2. 建立新的 Service Account，下載 JSON 金鑰
3. 將 JSON 金鑰檔案路徑填入 .env 的 GDRIVE_SERVICE_ACCOUNT_JSON
4. 在 Google Drive 資料夾上，將 Service Account 的 Email 加入「編輯者」

使用方式
--------
from drive_uploader import DriveUploader

uploader = DriveUploader.from_env("/path/to/.env")
# 上傳 JSON 字串
uploader.upload_json_string("eewrep_summary.json", json_string)
# 上傳本機檔案
uploader.upload_file(Path("2026_10/20261029_n1.rep"), parent_folder_id="...")
# 按月份歸檔上傳 .rep 檔案
uploader.upload_rep_to_monthly_folder(rep_path, report_datetime)
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = logging.getLogger("drive_uploader")

# ---------------------------------------------------------------------------
# 延遲載入 Google API 套件，讓沒安裝的環境也能 import 本模組（只是不能用）
# ---------------------------------------------------------------------------
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload, MediaFileUpload
    from google.oauth2 import service_account

    _GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    _GOOGLE_LIBS_AVAILABLE = False


SCOPES = ["https://www.googleapis.com/auth/drive"]
_MIME_JSON = "application/json"
_MIME_FOLDER = "application/vnd.google-apps.folder"
_MIME_OCTET = "application/octet-stream"


def _require_google_libs() -> None:
    if not _GOOGLE_LIBS_AVAILABLE:
        raise RuntimeError(
            "Google API 套件未安裝。請執行：\n"
            "pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )


def load_dotenv(path: Path) -> dict[str, str]:
    """從 .env 讀取 KEY=VALUE 設定（不依賴 python-dotenv）。"""
    if not path.is_file():
        raise RuntimeError(f"找不到 .env 檔案：{path}")
    values: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f".env 第 {lineno} 行格式錯誤（應為 KEY=VALUE）")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise RuntimeError(f".env 第 {lineno} 行引號不配對")
            value = value[1:-1]
        values[key] = value
    return values


class DriveUploader:
    """封裝 Google Drive API 的上傳工具類別。"""

    def __init__(self, service_account_json: str, root_folder_id: str) -> None:
        """
        Parameters
        ----------
        service_account_json : str
            Service Account JSON 金鑰檔案的完整路徑。
        root_folder_id : str
            要上傳到的 Google Drive 根資料夾 ID。
        """
        _require_google_libs()
        self._root_folder_id = root_folder_id
        credentials = service_account.Credentials.from_service_account_file(
            service_account_json, scopes=SCOPES
        )
        self._service = build("drive", "v3", credentials=credentials)
        # 快取 YYYY_MM 子資料夾 ID，避免重複查詢
        self._monthly_folder_cache: dict[str, str] = {}
        LOG.info("DriveUploader 初始化完成，根資料夾 ID: %s", root_folder_id)

    @classmethod
    def from_env(cls, env_path: Path | str) -> "DriveUploader":
        """從 .env 檔案自動讀取設定並建立實例。

        .env 必須包含：
            GDRIVE_SERVICE_ACCOUNT_JSON=...
            GDRIVE_FOLDER_ID=...
        """
        values = load_dotenv(Path(env_path))
        sa_json = values.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
        folder_id = values.get("GDRIVE_FOLDER_ID", "").strip()
        if not sa_json:
            raise RuntimeError(
                ".env 中缺少 GDRIVE_SERVICE_ACCOUNT_JSON，"
                "請填入 Service Account JSON 金鑰的完整路徑。"
            )
        if not folder_id:
            raise RuntimeError(
                ".env 中缺少 GDRIVE_FOLDER_ID，"
                "請填入 Google Drive 目標資料夾的 ID。"
            )
        return cls(sa_json, folder_id)

    # ------------------------------------------------------------------
    # 內部輔助方法
    # ------------------------------------------------------------------

    def _find_file_id(self, name: str, parent_id: str) -> str | None:
        """在指定資料夾內搜尋同名檔案，回傳 file ID（找不到則回傳 None）。"""
        query = (
            f"name = {json.dumps(name)} "
            f"and '{parent_id}' in parents "
            "and trashed = false"
        )
        result = (
            self._service.files()
            .list(q=query, fields="files(id, name)", pageSize=10)
            .execute()
        )
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def _find_or_create_folder(self, name: str, parent_id: str) -> str:
        """在 parent_id 資料夾下尋找或建立名稱為 name 的子資料夾，回傳其 ID。"""
        existing_id = self._find_file_id(name, parent_id)
        if existing_id:
            return existing_id
        metadata = {
            "name": name,
            "mimeType": _MIME_FOLDER,
            "parents": [parent_id],
        }
        folder = (
            self._service.files()
            .create(body=metadata, fields="id")
            .execute()
        )
        folder_id: str = folder["id"]
        LOG.info("已在 Drive 建立子資料夾：%s (id=%s)", name, folder_id)
        return folder_id

    def _upload_or_update(
        self,
        name: str,
        media,
        mime_type: str,
        parent_id: str,
    ) -> str:
        """若同名檔案已存在則更新內容，否則建立新檔案，回傳 file ID。"""
        existing_id = self._find_file_id(name, parent_id)
        if existing_id:
            result = (
                self._service.files()
                .update(fileId=existing_id, media_body=media, fields="id")
                .execute()
            )
            LOG.info("✅ [Drive] 已更新：%s", name)
            return result["id"]
        else:
            metadata: dict[str, Any] = {
                "name": name,
                "mimeType": mime_type,
                "parents": [parent_id],
            }
            result = (
                self._service.files()
                .create(body=metadata, media_body=media, fields="id")
                .execute()
            )
            LOG.info("✅ [Drive] 已建立：%s", name)
            return result["id"]

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def upload_json_string(
        self,
        filename: str,
        json_string: str,
        parent_folder_id: str | None = None,
    ) -> str:
        """將 JSON 字串以指定檔名上傳到 Drive（若已存在則覆蓋）。

        Parameters
        ----------
        filename : str
            目標檔名，例如 "eewrep_summary.json"。
        json_string : str
            要上傳的 JSON 字串內容。
        parent_folder_id : str, optional
            父資料夾 ID（預設使用根資料夾）。

        Returns
        -------
        str
            上傳後的 Drive file ID。
        """
        parent_id = parent_folder_id or self._root_folder_id
        media = MediaInMemoryUpload(
            json_string.encode("utf-8"), mimetype=_MIME_JSON, resumable=False
        )
        return self._upload_or_update(filename, media, _MIME_JSON, parent_id)

    def upload_file(
        self,
        local_path: Path,
        remote_name: str | None = None,
        parent_folder_id: str | None = None,
        mime_type: str = _MIME_OCTET,
    ) -> str:
        """將本機檔案上傳到 Drive（若已存在則覆蓋）。

        Parameters
        ----------
        local_path : Path
            要上傳的本機檔案路徑。
        remote_name : str, optional
            Drive 上的檔名（預設使用 local_path.name）。
        parent_folder_id : str, optional
            父資料夾 ID（預設使用根資料夾）。
        mime_type : str, optional
            MIME 類型（預設：application/octet-stream）。

        Returns
        -------
        str
            上傳後的 Drive file ID。
        """
        parent_id = parent_folder_id or self._root_folder_id
        name = remote_name or local_path.name
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
        return self._upload_or_update(name, media, mime_type, parent_id)

    def get_or_create_monthly_folder(self, year_month: str) -> str:
        """取得（或建立）根資料夾下的 YYYY_MM 子資料夾，回傳其 ID。

        Parameters
        ----------
        year_month : str
            格式為 "YYYY_MM"，例如 "2026_10"。
        """
        if year_month not in self._monthly_folder_cache:
            folder_id = self._find_or_create_folder(year_month, self._root_folder_id)
            self._monthly_folder_cache[year_month] = folder_id
        return self._monthly_folder_cache[year_month]

    def upload_rep_to_monthly_folder(
        self,
        rep_path: Path,
        report_time: datetime,
    ) -> str:
        """將 .rep 原始報告檔上傳至對應的 YYYY_MM 月份子資料夾。

        Parameters
        ----------
        rep_path : Path
            .rep 檔案的本機路徑。
        report_time : datetime
            報告時間（用來決定放入哪個 YYYY_MM 資料夾）。

        Returns
        -------
        str
            上傳後的 Drive file ID。
        """
        year_month = report_time.strftime("%Y_%m")
        folder_id = self.get_or_create_monthly_folder(year_month)
        return self.upload_file(
            rep_path,
            parent_folder_id=folder_id,
            mime_type="text/plain",
        )

