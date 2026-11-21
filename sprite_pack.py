"""弹丸论破像素小人本地持久化缓存、Drop-in 目录扫描与跨设备 .zip 图包导入导出（纯标准库）。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import time
import zipfile
from pathlib import Path
from typing import Any

from . import danganronpa_catalog

_MAX_IMAGE_BYTES = 1_048_576  # 单张图片上限 1MB
_MAX_ZIP_BYTES = 50 * 1024 * 1024  # 导出/导入 zip 上限 50MB
_EXT_TO_MIME = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
_MIME_TO_EXT = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
_ALL_EXTS = (".png", ".gif", ".webp")


def cache_dir(data_root: Path) -> Path:
    return Path(data_root) / "pixel_sprite_cache"


def dropin_dir(data_root: Path) -> Path:
    return Path(data_root) / "pixel_sprite_packs"


def manifest_path(data_root: Path) -> Path:
    return cache_dir(data_root) / "manifest.json"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, raw)


def _read_manifest(data_root: Path) -> dict[str, Any]:
    mp = manifest_path(data_root)
    if not mp.is_file():
        return {"version": 1, "entries": {}}
    try:
        doc = json.loads(mp.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("entries"), dict):
            return {"version": int(doc.get("version") or 1), "entries": dict(doc["entries"])}
    except (OSError, ValueError):
        pass
    return {"version": 1, "entries": {}}


def _inspect_image_bytes(raw: bytes) -> tuple[str, str, int, int] | None:
    """校验图片魔数与大小，并提取 (mime, ext, width, height)。非法返回 None。"""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 8 or len(raw) > _MAX_IMAGE_BYTES:
        return None
    b = bytes(raw)
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = (0, 0)
        if len(b) >= 24 and b[12:16] == b"IHDR":
            w, h = struct.unpack(">II", b[16:24])
        return ("image/png", ".png", int(w), int(h))
    if b[:6] in (b"GIF87a", b"GIF89a"):
        w, h = (0, 0)
        if len(b) >= 10:
            w, h = struct.unpack("<HH", b[6:10])
        return ("image/gif", ".gif", int(w), int(h))
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        w, h = (0, 0)
        chunk = b[12:16]
        if chunk == b"VP8X" and len(b) >= 30:
            w = 1 + int.from_bytes(b[24:27], "little")
            h = 1 + int.from_bytes(b[27:30], "little")
        elif chunk == b"VP8L" and len(b) >= 25 and b[20] == 0x2F:
            bits = int.from_bytes(b[21:25], "little")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8 " and len(b) >= 30 and b[23:26] == b"\x9d\x01\x2a":
            w_raw, h_raw = struct.unpack("<HH", b[26:30])
            w, h = (w_raw & 0x3FFF), (h_raw & 0x3FFF)
        return ("image/webp", ".webp", int(w), int(h))
    return None


def _canonical_key_for(name: str, english_name: str = "") -> tuple[str, dict[str, Any] | None]:
    entry = danganronpa_catalog.lookup_character(name) or (
        danganronpa_catalog.lookup_character(english_name) if english_name else None
    )
    if entry is not None:
        return str(entry["key"]), entry
    base = str(name or english_name or "actor").strip().lower()
    digest = hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"custom_{digest}", None


def _scan_dropin_files(data_root: Path) -> dict[str, dict[str, Any]]:
    """扫描 <data_root>/pixel_sprite_packs/（最多递归 2 层），按角色规范键索引可用图片。"""
    root = dropin_dir(data_root)
    found: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return found

    candidates: list[Path] = []
    try:
        for p in sorted(root.iterdir()):
            if p.name.startswith(".") or p.name == "__MACOSX":
                continue
            if p.is_file() and p.suffix.lower() in _ALL_EXTS:
                candidates.append(p)
            elif p.is_dir():
                for sub in sorted(p.iterdir()):
                    if sub.name.startswith("."):
                        continue
                    if sub.is_file() and sub.suffix.lower() in _ALL_EXTS:
                        candidates.append(sub)
                    elif sub.is_dir():
                        for sub2 in sorted(sub.iterdir()):
                            if not sub2.name.startswith(".") and sub2.is_file() and sub2.suffix.lower() in _ALL_EXTS:
                                candidates.append(sub2)
    except OSError:
        return found

    for fp in candidates:
        parent_stem = fp.parent.name if fp.parent != root else ""
        entry = (
            danganronpa_catalog.lookup_character(fp.stem)
            or (danganronpa_catalog.lookup_character(f"{parent_stem} {fp.stem}") if parent_stem else None)
            or (danganronpa_catalog.lookup_character(parent_stem) if parent_stem else None)
        )
        if entry is None:
            continue
        k = str(entry["key"])
        if k in found:
            continue
        found[k] = {"path": fp, "entry": entry}
    return found


def _build_sprite_payload(
    raw_bytes: bytes,
    *,
    key: str,
    title: str,
    english_name: str,
    source: str,
    width_hint: int = 0,
    height_hint: int = 0,
) -> dict[str, Any] | None:
    inspected = _inspect_image_bytes(raw_bytes)
    if inspected is None:
        return None
    mime, _ext, w_hdr, h_hdr = inspected
    w = int(width_hint or w_hdr or 0)
    h = int(height_hint or h_hdr or 0)
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return {
        "image_data_url": f"data:{mime};base64,{b64}",
        "title": title,
        "width": w,
        "height": h,
        "english_name": english_name,
        "key": key,
        "cached": True,
        "source": source,
    }


def get_cached_sprite(data_root: Path, name: str, english_name: str = "") -> dict[str, Any] | None:
    """优先从 pixel_sprite_cache 命中，其次扫描 pixel_sprite_packs 目录，零联网返回 data URL。"""
    key, entry = _canonical_key_for(name, english_name)
    cdir = cache_dir(data_root)
    manifest = _read_manifest(data_root)
    meta = manifest["entries"].get(key) if isinstance(manifest["entries"].get(key), dict) else None

    candidate_paths: list[Path] = []
    if meta and meta.get("file"):
        fname = Path(str(meta["file"])).name
        if fname:
            candidate_paths.append(cdir / fname)
    for ext in _ALL_EXTS:
        candidate_paths.append(cdir / f"{key}{ext}")

    seen_paths: set[Path] = set()
    for cp in candidate_paths:
        if cp in seen_paths or not cp.is_file():
            continue
        seen_paths.add(cp)
        try:
            raw = cp.read_bytes()
        except OSError:
            continue
        payload = _build_sprite_payload(
            raw,
            key=key,
            title=str((meta or {}).get("title") or cp.name),
            english_name=str((meta or {}).get("english_name") or (entry["en"] if entry else english_name)),
            source=str((meta or {}).get("source") or "cache"),
            width_hint=int((meta or {}).get("width") or 0),
            height_hint=int((meta or {}).get("height") or 0),
        )
        if payload is not None:
            return payload

    # Drop-in 目录扫描兜底
    if entry is not None:
        dropin = _scan_dropin_files(data_root)
        hit = dropin.get(key)
        if hit is not None:
            fp: Path = hit["path"]
            try:
                raw = fp.read_bytes()
            except OSError:
                return None
            return _build_sprite_payload(
                raw,
                key=key,
                title=fp.name,
                english_name=str(entry["en"]),
                source="dropin",
            )
    return None


def save_cached_sprite(
    data_root: Path,
    name: str,
    raw_bytes: bytes,
    *,
    mime: str = "",
    title: str = "",
    width: int = 0,
    height: int = 0,
    english_name: str = "",
    source: str = "wiki",
) -> dict[str, Any]:
    inspected = _inspect_image_bytes(raw_bytes)
    if inspected is None:
        raise ValueError("无效或超过 1MB 的像素图片文件（仅支持 PNG / GIF / WEBP）。")
    det_mime, det_ext, w_hdr, h_hdr = inspected
    eff_mime = mime if mime in _MIME_TO_EXT else det_mime
    eff_ext = _MIME_TO_EXT.get(eff_mime, det_ext)

    key, entry = _canonical_key_for(name, english_name)
    cdir = cache_dir(data_root)
    cdir.mkdir(parents=True, exist_ok=True)
    target = cdir / f"{key}{eff_ext}"

    for old_ext in _ALL_EXTS:
        if old_ext != eff_ext:
            stale = cdir / f"{key}{old_ext}"
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass

    _atomic_write_bytes(target, raw_bytes)

    eff_w = int(width or w_hdr or 0)
    eff_h = int(height or h_hdr or 0)
    eff_en = str(english_name or (entry["en"] if entry else "")).strip()
    eff_zh = str(entry["zh"] if entry else name).strip()
    eff_title = str(title or target.name).strip()

    manifest = _read_manifest(data_root)
    manifest["entries"][key] = {
        "key": key,
        "zh": eff_zh,
        "en": eff_en,
        "english_name": eff_en,
        "file": target.name,
        "mime": eff_mime,
        "width": eff_w,
        "height": eff_h,
        "title": eff_title,
        "source": str(source or "wiki"),
        "updated_at": round(time.time(), 3),
    }
    _atomic_write_json(manifest_path(data_root), manifest)

    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return {
        "image_data_url": f"data:{eff_mime};base64,{b64}",
        "title": eff_title,
        "width": eff_w,
        "height": eff_h,
        "english_name": eff_en,
        "key": key,
        "cached": True,
        "source": str(source or "wiki"),
    }


def save_cached_sprite_data_url(
    data_root: Path,
    name: str,
    data_url: str,
    *,
    source: str = "manual",
    title: str = "",
) -> dict[str, Any] | None:
    raw_str = str(data_url or "").strip()
    if not raw_str:
        return None
    mime_hint = ""
    b64_part = raw_str
    if raw_str.startswith("data:"):
        head, _, body = raw_str.partition(",")
        if not body or ";base64" not in head:
            return None
        mime_hint = head[5:].split(";", 1)[0].strip().lower()
        b64_part = body
    try:
        raw_bytes = base64.b64decode(b64_part, validate=True)
    except Exception:
        return None
    if _inspect_image_bytes(raw_bytes) is None:
        return None
    return save_cached_sprite(
        data_root,
        name,
        raw_bytes,
        mime=mime_hint,
        title=title or f"{name}.png",
        source=source,
    )


def list_cache_status(data_root: Path) -> dict[str, Any]:
    """统计全系列 56 位角色的本地缓存覆盖情况、各系列计数及自定义缓存角色。"""
    cdir = cache_dir(data_root)
    manifest = _read_manifest(data_root)
    entries_map: dict[str, Any] = manifest.get("entries") or {}
    dropin_map = _scan_dropin_files(data_root)

    valid_cached_meta: dict[str, dict[str, Any]] = {}
    for k, meta in entries_map.items():
        if not isinstance(meta, dict):
            continue
        fname = Path(str(meta.get("file") or "")).name
        if fname and (cdir / fname).is_file():
            valid_cached_meta[k] = meta
    if cdir.is_dir():
        for ext in _ALL_EXTS:
            for fp in cdir.glob(f"*{ext}"):
                k = fp.stem
                if k not in valid_cached_meta and _inspect_image_bytes(fp.read_bytes()) is not None:
                    valid_cached_meta[k] = {"key": k, "file": fp.name, "source": "cache", "width": 0, "height": 0}

    catalog = danganronpa_catalog.list_catalog()
    catalog_keys = {row["key"] for row in catalog}
    series_counts: dict[str, dict[str, int]] = {}
    items: list[dict[str, Any]] = []
    cached_count = 0

    for row in catalog:
        k = row["key"]
        ser = row["series"]
        sc = series_counts.setdefault(ser, {"cached": 0, "total": 0})
        sc["total"] += 1

        meta = valid_cached_meta.get(k)
        drop_hit = dropin_map.get(k)
        is_cached = meta is not None or drop_hit is not None
        if is_cached:
            cached_count += 1
            sc["cached"] += 1
        src = str((meta or {}).get("source") or ("dropin" if drop_hit is not None else ""))
        thumb_url = ""
        title_str = str((meta or {}).get("title") or "")
        w_val = int((meta or {}).get("width") or 0)
        h_val = int((meta or {}).get("height") or 0)
        thumb_path: Path | None = None
        if meta is not None and meta.get("file"):
            thumb_path = cdir / Path(str(meta["file"])).name
        elif drop_hit is not None:
            thumb_path = drop_hit["path"]
            if not title_str:
                title_str = thumb_path.name
        if thumb_path is not None and thumb_path.is_file():
            try:
                raw_t = thumb_path.read_bytes()
                insp_t = _inspect_image_bytes(raw_t)
                if insp_t is not None:
                    t_mime, _t_ext, tw, th = insp_t
                    thumb_url = f"data:{t_mime};base64,{base64.b64encode(raw_t).decode('ascii')}"
                    w_val = w_val or tw
                    h_val = h_val or th
            except OSError:
                pass
        items.append({
            "key": k,
            "zh": row["zh"],
            "en": row["en"],
            "series": ser,
            "cached": is_cached,
            "source": src,
            "title": title_str,
            "thumb_data_url": thumb_url,
            "width": w_val,
            "height": h_val,
        })

    extra_cached = [
        {
            "key": k,
            "zh": str(meta.get("zh") or k),
            "en": str(meta.get("en") or ""),
            "source": str(meta.get("source") or "cache"),
        }
        for k, meta in sorted(valid_cached_meta.items())
        if k not in catalog_keys
    ]

    return {
        "cached_count": cached_count,
        "total_catalog_count": len(catalog),
        "series_counts": series_counts,
        "items": items,
        "extra_cached": extra_cached,
        "cache_dir": str(cdir),
        "dropin_dir": str(dropin_dir(data_root)),
    }


def export_pack_zip_b64(data_root: Path) -> dict[str, Any]:
    """将本地所有有效缓存像素小人及清单打成可跨设备移植的 .zip 包（base64）。"""
    cdir = cache_dir(data_root)
    manifest = _read_manifest(data_root)
    entries_in: dict[str, Any] = manifest.get("entries") or {}

    files_to_pack: list[tuple[str, bytes, dict[str, Any]]] = []
    seen_keys: set[str] = set()

    for key, meta in sorted(entries_in.items()):
        if not isinstance(meta, dict):
            continue
        fname = Path(str(meta.get("file") or "")).name
        if not fname:
            continue
        fp = cdir / fname
        if not fp.is_file():
            continue
        try:
            raw = fp.read_bytes()
        except OSError:
            continue
        if _inspect_image_bytes(raw) is None:
            continue
        seen_keys.add(key)
        files_to_pack.append((fname, raw, dict(meta)))

    if cdir.is_dir():
        for ext in _ALL_EXTS:
            for fp in sorted(cdir.glob(f"*{ext}")):
                k = fp.stem
                if k in seen_keys:
                    continue
                try:
                    raw = fp.read_bytes()
                except OSError:
                    continue
                inspected = _inspect_image_bytes(raw)
                if inspected is None:
                    continue
                mime, _, w, h = inspected
                cat = danganronpa_catalog.lookup_character(k)
                seen_keys.add(k)
                files_to_pack.append((
                    fp.name,
                    raw,
                    {
                        "key": k,
                        "zh": cat["zh"] if cat else k,
                        "en": cat["en"] if cat else "",
                        "english_name": cat["en"] if cat else "",
                        "file": fp.name,
                        "mime": mime,
                        "width": w,
                        "height": h,
                        "title": fp.name,
                        "source": "pack",
                    },
                ))

    if not files_to_pack:
        raise ValueError("本地尚无已缓存的像素小人，可先联网预缓存或导入像素图后再导出。")

    export_manifest = {
        "version": 1,
        "exported_at": round(time.time(), 3),
        "entries": {meta["key"]: meta for _fn, _raw, meta in files_to_pack},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, raw, _meta in files_to_pack:
            zf.writestr(fname, raw)
        zf.writestr("manifest.json", json.dumps(export_manifest, ensure_ascii=False, indent=2) + "\n")

    zbytes = buf.getvalue()
    if len(zbytes) > _MAX_ZIP_BYTES:
        raise ValueError("导出图包超过 50MB 上限。")
    return {
        "filename": "danganronpa_pixel_pack.zip",
        "count": len(files_to_pack),
        "size_bytes": len(zbytes),
        "zip_b64": base64.b64encode(zbytes).decode("ascii"),
    }


def _decode_b64_payload(raw_or_data_url: str, max_bytes: int) -> bytes:
    s = str(raw_or_data_url or "").strip()
    if not s:
        raise ValueError("未提供压缩包数据。")
    if s.startswith("data:"):
        _, _, body = s.partition(",")
        s = body
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception as exc:
        raise ValueError("ZIP 数据 base64 解码失败。") from exc
    if len(raw) > max_bytes:
        raise ValueError("图包大小超过 50MB 上限。")
    return raw


def _is_unsafe_zip_member(name: str) -> bool:
    if not name or name.endswith("/") or name.endswith("\\"):
        return True
    norm = name.replace("\\", "/")
    if norm.startswith("/") or (len(norm) >= 2 and norm[1] == ":"):
        return True
    parts = [p for p in norm.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return True
    if any(p == "__MACOSX" or p.startswith(".") for p in parts):
        return True
    return False


def import_pack_zip_b64(data_root: Path, zip_b64_or_data_url: str) -> dict[str, Any]:
    """解包跨设备移植的 .zip 图包到 pixel_sprite_cache，支持 manifest 与文件名智能匹配。"""
    zbytes = _decode_b64_payload(zip_b64_or_data_url, _MAX_ZIP_BYTES)
    try:
        zf = zipfile.ZipFile(io.BytesIO(zbytes), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("上传的文件不是有效的 ZIP 压缩包。") from exc

    matched_chars: list[str] = []
    unmatched_files: list[str] = []
    skipped_invalid = 0

    with zf:
        # 1. 若压缩包内含 manifest.json，建立 file -> entry 元数据映射
        zip_manifest_by_file: dict[str, dict[str, Any]] = {}
        for info in zf.infolist():
            if _is_unsafe_zip_member(info.filename):
                continue
            if Path(info.filename).name.lower() == "manifest.json" and info.file_size <= _MAX_IMAGE_BYTES:
                try:
                    mdoc = json.loads(zf.read(info).decode("utf-8", errors="ignore"))
                    ent_map = mdoc.get("entries") if isinstance(mdoc, dict) else None
                    if isinstance(ent_map, dict):
                        for _k, m in ent_map.items():
                            if isinstance(m, dict) and m.get("file"):
                                zip_manifest_by_file[Path(str(m["file"])).name] = m
                except Exception:
                    pass

        # 2. 遍历所有成员文件
        for info in zf.infolist():
            if info.is_dir():
                continue
            norm_name = info.filename.replace("\\", "/")
            if norm_name.startswith("/") or (len(norm_name) >= 2 and norm_name[1] == ":"):
                skipped_invalid += 1
                continue
            parts = [p for p in norm_name.split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                skipped_invalid += 1
                continue
            if any(p == "__MACOSX" or p.startswith(".") for p in parts):
                continue

            base_name = parts[-1]
            ext = Path(base_name).suffix.lower()
            if base_name.lower() == "manifest.json":
                continue
            if ext not in _ALL_EXTS:
                skipped_invalid += 1
                continue
            if info.file_size > _MAX_IMAGE_BYTES:
                skipped_invalid += 1
                continue

            try:
                raw_img = zf.read(info)
            except Exception:
                skipped_invalid += 1
                continue
            if _inspect_image_bytes(raw_img) is None:
                skipped_invalid += 1
                continue

            m_meta = zip_manifest_by_file.get(base_name)
            cat_entry = None
            target_name = ""
            english_name = ""
            if m_meta:
                for probe in (m_meta.get("key"), m_meta.get("zh"), m_meta.get("en"), m_meta.get("english_name")):
                    if probe:
                        cat_entry = danganronpa_catalog.lookup_character(str(probe))
                        if cat_entry:
                            break
                target_name = str(m_meta.get("zh") or m_meta.get("key") or "")
                english_name = str(m_meta.get("en") or m_meta.get("english_name") or "")

            if cat_entry is None:
                stem = Path(base_name).stem
                parent_dir = parts[-2] if len(parts) >= 2 else ""
                cat_entry = (
                    danganronpa_catalog.lookup_character(stem)
                    or (danganronpa_catalog.lookup_character(f"{parent_dir} {stem}") if parent_dir else None)
                    or (danganronpa_catalog.lookup_character(parent_dir) if parent_dir else None)
                )

            if cat_entry is not None:
                save_cached_sprite(
                    data_root,
                    str(cat_entry["zh"]),
                    raw_img,
                    title=base_name,
                    english_name=str(cat_entry["en"]),
                    source="zip_pack",
                )
                matched_chars.append(str(cat_entry["zh"]))
            elif m_meta and target_name:
                save_cached_sprite(
                    data_root,
                    target_name,
                    raw_img,
                    title=base_name,
                    english_name=english_name,
                    source="zip_pack",
                )
                matched_chars.append(target_name)
            else:
                unmatched_files.append(base_name)

    return {
        "imported_count": len(matched_chars),
        "matched_characters": matched_chars,
        "unmatched_files": unmatched_files,
        "skipped_invalid": skipped_invalid,
        "status": list_cache_status(data_root),
    }
