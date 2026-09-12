import glob
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import hou


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def norm(p):
    return os.path.normcase(os.path.abspath(os.path.normpath(p)))


def package(source, archive_root, skip_cache_outputs=True, skip_render_outputs=True, categories=None, filter_red_nodes=False, filter_external_nodes=False, exclude_renderer_nodes=True, fixed_package_dir=None):
    source = os.path.abspath(source)
    source_dir = os.path.dirname(source)
    stem = Path(source).stem
    os.makedirs(archive_root, exist_ok=True)
    if fixed_package_dir:
        package_dir = os.path.abspath(fixed_package_dir)
        # A fixed directory is reused by GUI retry attempts. Keep resources
        # already copied by the preceding attempt so a retry cannot destroy a
        # nearly complete package or repeat large transfers.
        os.makedirs(package_dir, exist_ok=True)
    else:
        package_dir = os.path.join(archive_root, stem)
        idx = 2
        while os.path.exists(package_dir):
            package_dir = os.path.join(archive_root, "%s_%02d" % (stem, idx))
            idx += 1
        os.makedirs(package_dir)

    source_norm = norm(source_dir)
    standard_dirs = {"geo", "abc", "render", "anim", "otls", "galleries", "textures", "audio", "scripts", "cache"}
    def inside(p):
        try:
            return os.path.commonpath([source_norm, norm(p)]) == source_norm
        except ValueError:
            return False

    def standard_rel(p):
        """Keep resources under a Houdini project standard folder in that folder."""
        parts = list(Path(os.path.normpath(p)).parts)
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].lower() == "houdini" and i + 1 < len(parts) and parts[i + 1].lower() in standard_dirs:
                return "/".join(parts[i + 1:])
        return None

    def builtin(p):
        q = norm(p)
        return q.startswith(norm(r"C:\Program Files\Side Effects Software")) or q.startswith(norm(r"C:\PROGRA~1\SIDEEF~1"))

    def copy_item(src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            copied_files = 0
            for current_root, dir_names, file_names in os.walk(src):
                relative_root = os.path.relpath(current_root, src)
                target_root = dst if relative_root == "." else os.path.join(dst, relative_root)
                os.makedirs(target_root, exist_ok=True)
                for dir_name in dir_names:
                    os.makedirs(os.path.join(target_root, dir_name), exist_ok=True)
                for file_name in file_names:
                    copied_files += copy_item(
                        os.path.join(current_root, file_name),
                        os.path.join(target_root, file_name),
                    )
            return copied_files
        elif os.path.isfile(src):
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copy2(src, dst)
                return 1
        return 0

    def safe_name(p):
        return re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(p)) or "resource"

    def expand(raw):
        try:
            return hou.text.expandString(raw)
        except Exception:
            return raw

    def evaluate_reference(parm, raw):
        """Resolve a parameter expression to its current concrete value."""
        if parm is not None:
            try:
                value = parm.evalAsString()
                if value:
                    return value
            except Exception:
                pass
        return expand(raw)

    def classify_resource(raw, expanded):
        text = (str(raw) + " " + str(expanded)).lower().replace("\\", "/")
        if text.endswith((".hda", ".hdalc", ".hdanc")) or "/otls/" in text:
            return "HDAs"
        if text.endswith((".usd", ".usda", ".usdc", ".usdz")):
            return "USDs"
        if text.endswith(".abc"):
            return "Alembics"
        if text.endswith((".bgeo", ".bgeo.sc", ".sim", ".vdb", ".pc", ".pdc", ".obj", ".fbx", ".ply", ".stl")):
            return "Geometry"
        if text.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".hdr", ".rat", ".tx", ".bmp", ".gif")):
            return "Image/Textures"
        path_markers = text.rstrip("/").split("/")
        if "otls" in path_markers:
            return "HDAs"
        if "usd" in path_markers or "usds" in path_markers:
            return "USDs"
        if "abc" in path_markers or "alembic" in path_markers:
            return "Alembics"
        if "geo" in path_markers or "geometry" in path_markers:
            return "Geometry"
        if "textures" in path_markers or "texture" in path_markers:
            return "Image/Textures"
        if text.endswith((".hda", ".hdalc", ".hdanc")) or "/otls/" in text:
            return "HDAs"
        if text.endswith((".usd", ".usda", ".usdc", ".usdz")):
            return "USDs"
        if text.endswith(".abc"):
            return "Alembics"
        if text.endswith((".bgeo", ".bgeo.sc", ".sim", ".vdb", ".pc", ".pdc", ".obj", ".fbx", ".ply", ".stl")):
            return "Geometry"
        if text.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".hdr", ".rat", ".tx", ".bmp", ".gif")):
            return "Image/Textures"
        return None

    def classify_directory_resource(expanded):
        """Classify a directory from the resources it directly contains."""
        if not os.path.isdir(expanded):
            return None
        try:
            extensions = {Path(entry.name).suffix.lower() for entry in os.scandir(expanded) if entry.is_file()}
        except OSError:
            return None
        if ".abc" in extensions:
            return "Alembics"
        return None

    category_dirs = {"Image/Textures": "textures", "Geometry": "geo", "Alembics": "abc", "USDs": "usd", "HDAs": "otls"}

    def storage_info(expanded, category, directory_hint=False):
        """Return the package-relative target and provenance metadata."""
        external = is_external(expanded)
        if not external:
            rel = os.path.relpath(expanded, source_dir).replace("\\", "/")
            return {"target": rel, "storage_category": category, "storage_directory": rel.rsplit("/", 1)[0] if "/" in rel else ".", "source_is_external": False, "source_hash": None, "collision_group": None}
        source_path = norm(expanded)
        source_dir_for_hash = source_path if directory_hint or os.path.isdir(expanded) else os.path.dirname(source_path)
        source_hash = hashlib.sha1(source_dir_for_hash.encode("utf-8", "replace")).hexdigest()[:8]
        source_folder = safe_name(source_dir_for_hash)
        collision_group = "%s_%s" % (source_folder, source_hash)
        if category in category_dirs:
            root = category_dirs[category]
            if directory_hint or os.path.isdir(expanded):
                target = "%s/%s" % (root, collision_group)
                storage_directory = target
            else:
                target = "%s/%s/%s" % (root, collision_group, safe_name(expanded))
                storage_directory = "%s/%s" % (root, collision_group)
            return {"target": target, "storage_category": category, "storage_directory": storage_directory, "source_is_external": True, "source_hash": source_hash, "collision_group": collision_group}
        storage_directory = "external/%s" % collision_group
        target = storage_directory if directory_hint or os.path.isdir(expanded) else "%s/%s" % (storage_directory, safe_name(expanded))
        return {"target": target, "storage_category": None, "storage_directory": storage_directory, "source_is_external": True, "source_hash": source_hash, "collision_group": collision_group}

    def is_red_node(parm):
        if parm is None:
            return False
        try:
            r, g, b = tuple(parm.node().color().rgb())
            return r >= 0.5 and r >= g * 1.35 and r >= b * 1.35
        except Exception:
            return False

    def is_render_gallery_reference(parm, raw=""):
        if parm is None:
            return False
        try:
            text = " ".join((parm.node().type().name().lower(), parm.node().name().lower(), parm.name().lower(), str(raw).lower()))
            return "rendergallery" in text or "gallerysource" in text or os.path.basename(str(raw).replace("\\", "/")).lower() == "rendergallery.db"
        except Exception:
            return False

    def is_renderer_node(parm, raw=""):
        if parm is None:
            return False
        try:
            node = parm.node()
            node_type = node.type().name().lower()
            node_name = node.name().lower()
            parm_name = parm.name().lower()
            category = node.type().category().name().lower()
            text = " ".join((node_type, node_name, parm_name, str(raw).lower()))
            renderer_words = ("render", "mantra", "karma", "arnold", "redshift", "renderman", "usdrender", "vray", "octane")
            render_parms = ("outputimage", "vm_picture", "picture", "soho_output", "output_file", "renderoutput", "cryptopicture")
            # Render Gallery is stored on /stage as a Manager node, not as a
            # Driver/LOP renderer node. Treat its source parameter/database as
            # renderer metadata and exclude the whole node from the archive.
            gallery_ref = is_render_gallery_reference(parm, raw)
            return gallery_ref or (category in {"driver", "lop"} and any(word in text for word in renderer_words)) or any(word in node_type for word in renderer_words) or parm_name in render_parms
        except Exception:
            return False

    def is_external(expanded):
        return os.path.isabs(expanded) and not inside(expanded)

    def is_filecache_node(node):
        try:
            return node.type().name().lower().startswith("filecache")
        except Exception:
            return False

    def filecache_has_input(node):
        try:
            return any(input_node is not None for input_node in node.inputs())
        except Exception:
            return False

    def output_kind(parm, raw, expanded):
        if parm is None:
            return None
        node = parm.node()
        node_type = node.type().name().lower()
        node_name = node.name().lower()
        parm_name = parm.name().lower()
        try:
            category = node.type().category().name().lower()
        except Exception:
            category = ""
        text = " ".join((node_type, node_name, parm_name, raw.lower()))
        filecache_node = is_filecache_node(node)
        if filecache_node:
            # File Cache 2.0 exposes many helper parameters as file
            # references. Only sopoutput is the actual cache dependency.
            # A cache node with no input is a read-only cache source and must
            # remain packageable even when cache outputs are skipped.
            if parm_name != "sopoutput":
                return "cache_auxiliary"
            if skip_cache_outputs and filecache_has_input(node):
                return "cache_output"
        cache_node = (not filecache_node) and any(x in text for x in ("filecache", "rop_geometry", "geometrycache", "dopimport", "alembiccache"))
        cache_parm = (not filecache_node) and parm_name in {"basedir", "filepattern", "cachefile", "cachefilename", "simfile", "sopoutput"}
        render_node = is_renderer_node(parm, raw)
        render_parm = parm_name in {"outputimage", "vm_picture", "picture", "cryptopicture", "soho_output", "output_file", "renderoutput"}
        local_path = expanded.replace("\\", "/").lower()
        cache_ext = local_path.endswith((".bgeo", ".bgeo.sc", ".sim", ".vdb", ".pc", ".pdc"))
        render_ext = local_path.endswith((".exr", ".png", ".jpg", ".jpeg", ".tif", ".tiff"))
        # A path under geo/cache/sim can be either a cache *input* (for
        # example a SOP File node) or a cache *output*. Only skip it when the
        # node/parameter identifies a writer; otherwise valid input caches are
        # incorrectly discarded.
        local_cache_path = inside(expanded) and cache_ext and (cache_node or cache_parm) and any(x in local_path for x in ("/geo/", "/cache/", "/sim/"))
        # Apply the same distinction to render folders: a File node reading
        # an internal plate is a dependency, not a render output.
        local_render_path = inside(expanded) and render_ext and (render_node or render_parm) and "/render/" in local_path
        if skip_cache_outputs and (cache_node or cache_parm):
            return "cache_output"
        if skip_cache_outputs and local_cache_path:
            return "cache_output"
        if skip_render_outputs and (render_node or render_parm):
            return "render_output"
        if skip_render_outputs and local_render_path:
            return "render_output"
        return None

    emit("status", message="加载 HIP：" + source)
    hou.hipFile.load(source, suppress_save_prompt=True, ignore_load_warnings=True)
    emit("status", message="正在扫描 HIP 文件引用…")
    scan_state = {"done": False}
    scan_started = time.monotonic()

    def report_scan_progress():
        while not scan_state["done"]:
            time.sleep(1.0)
            if not scan_state["done"]:
                elapsed = int(time.monotonic() - scan_started)
                emit("status", message="仍在扫描 HIP 文件引用… 已用时 %d 秒" % elapsed)

    scan_thread = threading.Thread(target=report_scan_progress, daemon=True)
    scan_thread.start()
    try:
        refs = hou.fileReferences()
    finally:
        scan_state["done"] = True
        scan_thread.join(0.2)
    refs = list(refs)
    ref_parm_paths = {parm.path() for parm, _ in refs if parm is not None}
    filecache_outputs = 0
    for node in hou.node("/").allSubChildren():
        node_type = node.type().name().lower()
        if not node_type.startswith("filecache"):
            continue
        output_parm = node.parm("sopoutput")
        if output_parm is None or output_parm.path() in ref_parm_paths:
            continue
        try:
            output_value = output_parm.evalAsString()
        except Exception:
            output_value = ""
        if output_value:
            refs.append((output_parm, output_value))
            ref_parm_paths.add(output_parm.path())
            filecache_outputs += 1
            emit("status", message="识别 File Cache 输出：%s -> %s" % (node.path(), output_value))
    emit("status", message="扫描到 %d 个文件引用" % len(refs))
    manifest, missing = [], []
    failures = []
    copied_folder_targets = set()
    verified = 0
    copied = rewritten = 0
    filtered_cache = filtered_render = filtered_renderer = 0
    filtered_category = filtered_node = 0
    category_counts = {}
    render_gallery_node_paths = set()
    if exclude_renderer_nodes:
        for ref_parm, ref_raw in refs:
            if is_render_gallery_reference(ref_parm, ref_raw):
                try:
                    render_gallery_node_paths.add(ref_parm.node().path())
                except Exception:
                    pass

    for parm, raw in refs:
        if not raw or not isinstance(raw, str):
            continue
        pname = parm.name().lower() if parm is not None else ""
        expanded = evaluate_reference(parm, raw)
        try:
            node_path = parm.node().path() if parm is not None else None
        except Exception:
            node_path = None
        renderer_node = is_renderer_node(parm, raw) or node_path in render_gallery_node_paths
        if exclude_renderer_nodes and renderer_node:
            filtered_renderer += 1
            manifest.append({"parm": parm.path() if parm else None, "node": node_path, "raw": raw, "expanded": expanded, "filtered_by": "renderer_node", "renderer_node": True})
            emit("status", message="排除渲染器/Render Gallery 节点：%s" % (node_path or (parm.path() if parm else raw)))
            continue
        kind = output_kind(parm, raw, expanded)
        if pname == "savetodirectory_directory" or kind:
            if kind == "cache_output":
                filtered_cache += 1
            elif kind == "render_output":
                filtered_render += 1
            manifest.append({"parm": parm.path() if parm else None, "raw": raw, "kind": kind or "output_directory_skipped"})
            emit("status", message="过滤 %s：%s" % (kind or "输出目录", parm.path() if parm else raw))
            continue
        category = classify_resource(raw, expanded) or classify_directory_resource(expanded)
        red_node = is_red_node(parm)
        external_node = is_external(expanded)
        if category:
            category_counts[category] = category_counts.get(category, 0) + 1
        storage_meta = storage_info(expanded, category) if os.path.isabs(expanded) else {"target": None, "storage_category": category, "storage_directory": None, "source_is_external": False, "source_hash": None, "collision_group": None}
        item_meta = {"parm": parm.path() if parm else None, "raw": raw, "expanded": expanded, "category": category, "red_node": red_node, "renderer_node": renderer_node, "external_resource": external_node, **storage_meta}
        if category and categories is not None and category not in categories:
            item_meta["filtered_by"] = "category"
            manifest.append(item_meta)
            filtered_category += 1
            emit("status", message="按资源分类过滤：%s | %s" % (category, parm.path() if parm else raw))
            continue
        if filter_red_nodes or filter_external_nodes:
            matches = (not filter_red_nodes or red_node) and (not filter_external_nodes or external_node)
            if not matches:
                item_meta["filtered_by"] = "node_rule"
                manifest.append(item_meta)
                filtered_node += 1
                emit("status", message="按节点规则过滤：%s | red=%s external=%s" % (parm.path() if parm else raw, red_node, external_node))
                continue
        if expanded.startswith(("temp:", "op:")) or not os.path.isabs(expanded):
            continue
        if builtin(expanded):
            builtin_item = dict(item_meta)
            builtin_item["kind"] = "houdini_builtin"
            manifest.append(builtin_item)
            continue

        # Handle Houdini expressions as sequences before the concrete current
        # value check. evalAsString() resolves file19/file22 to 002/003, but
        # the referenced parameter still represents a sequence expression and
        # all matching files should be archived.
        pattern_raw = re.sub(r"`[^`]+`", "*", raw) if "`" in raw else ""
        pattern = expand(pattern_raw) if pattern_raw else ""
        matches = glob.glob(pattern) if pattern else []
        if matches and parm is not None:
            sequence_meta = storage_info(os.path.dirname(expanded), category, directory_hint=True)
            rel_dir = sequence_meta["target"]
            emit("status", message="复制序列：%s（%d 个文件）" % (rel_dir, len(matches)))
            try:
                for match in matches:
                    copied += copy_item(match, os.path.join(package_dir, rel_dir, os.path.basename(match)))
            except Exception as exc:
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel_dir, "reason": "copy_failed", "detail": str(exc)})
                emit("status", message="复制失败：%s" % raw)
                continue
            if not all(os.path.exists(os.path.join(package_dir, rel_dir, os.path.basename(match))) for match in matches):
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel_dir, "reason": "target_missing"})
                continue
            rel_value = rel_dir + "/" + os.path.basename(expanded)
            rel_value = "$HIP/" + rel_value.lstrip("/")
            try:
                parm.set(rel_value)
            except Exception as exc:
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel_dir, "reason": "rewrite_failed", "detail": str(exc)})
                continue
            rewritten += 1
            verified += 1
            item = dict(item_meta)
            item.update(sequence_meta)
            item.update({"target": rel_dir, "rewritten": rel_value, "sequence_matches": len(matches)})
            manifest.append(item)
            continue

        if os.path.exists(expanded):
            rel = storage_meta["target"]
            if storage_meta.get("source_is_external") and category:
                emit("status", message="外部资源分类：%s | 归档目录：%s | 复制资源：%s" % (category, storage_meta.get("storage_directory"), os.path.basename(expanded)))
            elif storage_meta.get("source_is_external"):
                emit("status", message="未识别标准类型，归档到 external | 复制资源：%s" % os.path.basename(expanded))
            else:
                emit("status", message="复制资源：%s -> %s" % (expanded, rel))
            target_path = os.path.join(package_dir, rel.replace("/", os.sep))
            copy_source = expanded
            copy_target = target_path
            copied_parent_directory = False
            if storage_meta.get("source_is_external") and category == "Alembics" and os.path.isfile(expanded):
                copy_source = os.path.dirname(expanded)
                copy_target = os.path.dirname(target_path)
                copied_parent_directory = True
                emit("status", message="按文件夹复制 Alembic：%s -> %s" % (copy_source, storage_meta.get("storage_directory")))
            folder_copy_key = (norm(copy_source), norm(copy_target)) if os.path.isdir(copy_source) else None
            folder_reused = folder_copy_key in copied_folder_targets if folder_copy_key else False
            try:
                copied_now = 0 if folder_reused else copy_item(copy_source, copy_target)
                if folder_copy_key:
                    copied_folder_targets.add(folder_copy_key)
            except Exception as exc:
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel, "reason": "copy_failed", "detail": str(exc)})
                emit("status", message="复制失败：%s" % expanded)
                continue
            if not os.path.exists(target_path):
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel, "reason": "target_missing"})
                continue
            copied += copied_now
            item = dict(item_meta)
            item["target"] = rel
            if copied_parent_directory:
                item["copied_parent_directory"] = True
                item["source_parent"] = copy_source
                item["folder_reused"] = folder_reused
            if parm is not None and (os.path.isabs(raw) or expanded != raw):
                if not (inside(expanded) and ("$HIP" in raw or "$JOB" in raw)):
                    hip_rel = "$HIP/" + rel.lstrip("/")
                    try:
                        parm.set(hip_rel)
                    except Exception as exc:
                        failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel, "reason": "rewrite_failed", "detail": str(exc)})
                        continue
                    rewritten += 1
                    item["rewritten"] = hip_rel
            verified += 1
            manifest.append(item)
            continue

        missing_item = dict(item_meta)
        missing_item["reason"] = "not found"
        missing.append(missing_item)
        failures.append({"node": node_path, "parameter": parm.path() if parm else None, "resource": raw, "target": storage_meta.get("target"), "reason": "not_found"})
        emit("status", message="未找到资源：" + raw)

    emit("status", message="保存归档 HIP…")
    dest = os.path.join(package_dir, os.path.basename(source))
    hou.hipFile.save(dest, save_to_recent_files=False)
    manifest_path = os.path.join(package_dir, "package_manifest.json")
    status = "success" if not failures else "failed"
    data = {
        "version": 2,
        "source": source,
        "package": dest,
        "status": status,
        "summary": {
            "references_seen": len(refs),
            "resources_copied": copied,
            "parameters_rewritten": rewritten,
            "verified": verified,
            "failed": len(failures),
            "filtered": {
                "cache_outputs": filtered_cache,
                "render_outputs": filtered_render,
                "renderer_nodes": filtered_renderer,
                "category": filtered_category,
                "node_rule": filtered_node,
            },
            "category_counts": category_counts,
            "not_found": len(missing),
        },
        "resources": manifest,
        "failures": failures,
    }
    manifest_temp_path = manifest_path + ".tmp"
    with open(manifest_temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(manifest_temp_path, manifest_path)
    emit("done", package=dest, manifest=manifest_path, status=status, failures=len(failures), resources_copied=copied, parameters_rewritten=rewritten, filtered_cache_outputs=filtered_cache, filtered_render_outputs=filtered_render, filtered_renderer_nodes=filtered_renderer, filtered_category=filtered_category, filtered_node_rule=filtered_node, category_counts=category_counts, not_found=len(missing))


if __name__ == "__main__":
    options = sys.argv[3:]
    categories = None
    if "--categories" in options:
        pos = options.index("--categories")
        categories = set(options[pos + 1].split(",")) if pos + 1 < len(options) and options[pos + 1] else set()
    # Renderer and Render Gallery nodes are always excluded automatically.
    fixed_package_dir = None
    if "--package-dir" in options:
        pos = options.index("--package-dir")
        if pos + 1 < len(options):
            fixed_package_dir = options[pos + 1]
    package(sys.argv[1], sys.argv[2], "--include-cache-outputs" not in options, "--include-render-outputs" not in options, categories, "--filter-red-nodes" in options, "--filter-external" in options, fixed_package_dir=fixed_package_dir)
