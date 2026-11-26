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

from hip_version import major_minor


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def norm(p):
    return os.path.normcase(os.path.abspath(os.path.normpath(p)))


# Scripts, configs and documents that a scene may point at but that are not
# assets. They are skipped by default; --include-auxiliary-files keeps them.
AUXILIARY_EXTENSIONS = {
    ".json", ".py", ".pyc", ".pyo", ".txt", ".log", ".md",
    ".xml", ".yaml", ".yml", ".csv", ".ini", ".cfg", ".toml",
    ".bat", ".cmd", ".sh", ".ps1",
}


def is_auxiliary_file(path):
    return os.path.splitext(str(path))[1].lower() in AUXILIARY_EXTENSIONS


# A reference is a sequence when it carries a backtick expression or a Houdini
# frame variable ($F, $F4, $SF, $SF4, $FF). Both are resolved to one concrete
# frame by evalAsString(), so they must be recognised on the *raw* string
# before any expansion happens. Missing them silently archives a single frame
# and rewrites the parameter to that fixed frame number.
SEQUENCE_TOKEN = re.compile(r"`[^`]+`|\$(?:SF\d?|F\d?|FF)(?![A-Za-z0-9_])")


# A cache path that already evaluated to one frame (`..._v1.0149.bgeo.sc`)
# still carries the frame number as the last digit run of its leaf. That run is
# the only way back to the sequence when the parameter itself no longer holds a
# token, so it is matched as prefix / digits / suffix.
FRAME_DIGITS_LEAF = re.compile(r"^(.*[._-])(\d+)(\D*)$")


# Pieces of a cache-name template that cannot be resolved from Python at all
# (backtick expressions, $OS and friends). They become wildcards when the
# template is compared against a concrete file name; only the literal parts and
# the frame number have to agree.
TEMPLATE_PART = re.compile(r"`[^`]+`|\$[A-Za-z_][A-Za-z0-9_]*")
FRAME_WIDTH = re.compile(r"^\$(?:SF|F|FF)(\d?)$")


def looks_like_path(value):
    """True when a parameter value is a file path rather than expression code.

    ``Parm.unexpandedString()`` returns the source text for an expression
    parameter, which must never be treated as a resource path.
    """
    text = str(value).strip()
    if not text or "\n" in text or "\r" in text:
        return False
    if text.startswith(("{", "return ", "import ")) or "chs(" in text or "ch(" in text:
        return False
    return "/" in text or "\\" in text


def build_archive_log(data):
    """Return a short, human-readable summary of one archive run."""
    summary = data.get("summary") or {}
    filtered = summary.get("filtered") or {}
    result = "成功" if data.get("status") == "success" else "失败"
    lines = [
        "Houdini 归档记录",
        "结果：%s" % result,
        "源工程：%s" % (data.get("source") or "-"),
        "归档工程：%s" % (data.get("package") or "-"),
        "资源：复制 %d，复用 %d，改写路径 %d，失败 %d，跳过 %d" % (
            summary.get("resources_copied", 0),
            summary.get("references_reused", 0),
            summary.get("parameters_rewritten", 0),
            summary.get("failed", 0),
            sum(filtered.values()),
        ),
    ]

    houdini = data.get("houdini") or {}
    if houdini:
        saved_with = houdini.get("saved_with") or "未知"
        opened_with = houdini.get("opened_with") or "未知"
        note = "" if houdini.get("version_match") else "（版本不一致）"
        lines.append("Houdini：保存 %s / 打开 %s%s" % (saved_with, opened_with, note))

    category_counts = summary.get("category_counts") or {}
    if category_counts:
        lines.append("分类：" + "，".join("%s %s" % item for item in sorted(category_counts.items())))

    exported = []
    seen = set()
    for item in data.get("resources") or []:
        target = item.get("storage_directory") if item.get("copied_parent_directory") else item.get("target")
        if target and target not in seen:
            seen.add(target)
            exported.append(target)
    lines.extend(["", "导出内容：", "- " + os.path.basename(data.get("package") or "归档工程")])
    lines.extend("- " + target for target in exported)
    if not exported:
        lines.append("- 无外部资源")

    duplicates = data.get("duplicates") or []
    if duplicates:
        lines.extend(["", "重复归档（同一源文件放到了多个位置，会各占一份空间）："])
        for item in duplicates:
            lines.append("- %s -> %s" % (item.get("source") or "未知", "，".join(item.get("targets") or [])))

    skipped_rewrites = summary.get("rewrites_skipped", 0)
    if skipped_rewrites:
        lines.extend(["", "保留原表达式的参数：%d 个（File Cache 这类锁定资产的输出参数改不动，靠 $HIP 相对路径定位到归档副本）" % skipped_rewrites])

    failures = data.get("failures") or []
    if failures:
        lines.extend(["", "失败项："])
        for failure in failures:
            subject = failure.get("resource") or failure.get("parameter") or failure.get("node") or "未知项目"
            lines.append("- %s（%s）" % (subject, failure.get("reason") or "unknown"))
    return "\n".join(lines) + "\n"


def package(source, archive_root, skip_cache_outputs=True, skip_render_outputs=True, categories=None, filter_red_nodes=False, filter_external_nodes=False, exclude_renderer_nodes=True, fixed_package_dir=None, skip_auxiliary_files=True, expect_version=""):
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

    def covers_source_root(p):
        """True when p is the project folder or one of its ancestors."""
        try:
            return os.path.commonpath([source_norm, norm(p)]) == norm(p)
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

    folder_auxiliary_skipped = 0
    # Copying is deduplicated by target path: several nodes pointing at the
    # same cache all resolve to one $HIP destination, so the bytes are written
    # once. Path-level dedup alone cannot answer two questions, which is what
    # these structures are for:
    #   * how many files were really written vs. resolved to an existing copy
    #   * whether one physical file was forced into two archive locations
    archived_sources = {}  # norm(realpath(src)) -> {"source": src, "targets": {rel, ...}}

    def archive_rel(dst):
        return os.path.relpath(dst, package_dir).replace("\\", "/")

    def note_archived_file(src, dst):
        """Record which archive path holds src (realpath collapses links)."""
        try:
            key = norm(os.path.realpath(src))
        except OSError:
            key = norm(src)
        entry = archived_sources.setdefault(key, {"source": src, "targets": set()})
        entry["targets"].add(archive_rel(dst))

    def same_archived_file(src, dst):
        """True when dst already holds an up-to-date copy of src.

        Size alone is not a safe judge: a retry reuses the package directory,
        so a cache that was rewritten to the same byte count would be kept
        stale forever. copy2 preserves mtime, which makes size + mtime the
        right pair. The tolerance covers FAT and network shares that store
        timestamps with two second granularity.
        """
        try:
            if not os.path.isfile(dst):
                return False
            source_stat = os.stat(src)
            target_stat = os.stat(dst)
        except OSError:
            return False
        if source_stat.st_size != target_stat.st_size:
            return False
        return abs(source_stat.st_mtime - target_stat.st_mtime) <= 2.0

    def copy_item(src, dst):
        """Copy src to dst, returning (files written, references reused)."""
        nonlocal folder_auxiliary_skipped
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            copied_here = reused_here = 0
            for current_root, dir_names, file_names in os.walk(src):
                relative_root = os.path.relpath(current_root, src)
                target_root = dst if relative_root == "." else os.path.join(dst, relative_root)
                os.makedirs(target_root, exist_ok=True)
                for dir_name in dir_names:
                    os.makedirs(os.path.join(target_root, dir_name), exist_ok=True)
                for file_name in file_names:
                    if skip_auxiliary_files and is_auxiliary_file(file_name):
                        folder_auxiliary_skipped += 1
                        continue
                    copied_count, reused_count = copy_item(
                        os.path.join(current_root, file_name),
                        os.path.join(target_root, file_name),
                    )
                    copied_here += copied_count
                    reused_here += reused_count
            return copied_here, reused_here
        elif os.path.isfile(src):
            note_archived_file(src, dst)
            if same_archived_file(src, dst):
                # Already archived by an earlier reference (or a previous
                # retry attempt). Counted as a reuse so resources_copied stays
                # an honest measure of data actually written.
                return 0, 1
            shutil.copy2(src, dst)
            return 1, 0
        return 0, 0

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

    def sequence_pattern(raw):
        """Glob pattern for a sequence reference, or "" when it is static.

        Only the sequence tokens become wildcards; $HIP/$JOB style variables
        are expanded afterwards, which is safe because no frame token is left
        for expandString() to resolve into a single frame.
        """
        if not SEQUENCE_TOKEN.search(str(raw)):
            return ""
        wildcarded = SEQUENCE_TOKEN.sub("*", str(raw))
        # Tokens sitting next to each other (a backtick plus $F) would produce
        # two wildcard runs inside one file name; collapse them.
        wildcarded = re.sub(r"\*+", "*", wildcarded)
        pattern = expand(wildcarded)
        # A parameter that is *only* an expression (File Cache 2.0's read_back
        # holds `chs("../sopoutput")`) would wildcard down to "*" and glob the
        # process working directory. Never glob a pattern without a directory.
        return pattern if os.path.isabs(pattern) else ""

    def sequence_basename(raw, expanded):
        """Archive-side file name, keeping the frame token when there is one.

        The raw name is kept verbatim instead of expanded to the current
        frame: $HIPNAME/$OS must keep resolving per node at open time, and
        only the frame part may stay dynamic.
        """
        name = str(raw).replace("\\", "/").rsplit("/", 1)[-1]
        if name and SEQUENCE_TOKEN.search(name):
            return name
        return os.path.basename(expanded)

    def hip_path(rel_dir, name):
        rel_dir = (rel_dir or ".").strip("/")
        if rel_dir in ("", "."):
            return "$HIP/" + name
        return "$HIP/%s/%s" % (rel_dir, name)

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
            renderer_words = ("mantra", "karma", "arnold", "redshift", "renderman", "usdrender", "vray", "octane")
            render_parms = ("outputimage", "vm_picture", "picture", "soho_output", "output_file", "renderoutput", "cryptopicture")
            # Render Gallery is stored on /stage as a Manager node, not as a
            # Driver/LOP renderer node. Treat its source parameter/database as
            # renderer metadata and exclude the whole node from the archive.
            gallery_ref = is_render_gallery_reference(parm, raw)
            # "render" alone is too broad as a substring: many non-renderer
            # nodes carry it in their type name. Only treat a bare "render"
            # as a renderer signal when the node already lives in a
            # driver/lop category, otherwise require a concrete renderer
            # brand or a render output parameter.
            return (
                gallery_ref
                or (category in {"driver", "lop"} and ("render" in text or any(word in text for word in renderer_words)))
                or any(word in node_type for word in renderer_words)
                or parm_name in render_parms
            )
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

    def inside_filecache(node):
        """True when node sits inside a File Cache HDA (its read_back network).

        File Cache 2.0 builds helper nodes inside the HDA — ``read_back`` is a
        plain File SOP holding ``chs("../sopoutput")``. Those nodes read the
        cache the HDA writes, so they belong to the cache-output decision
        instead of being treated as ordinary input dependencies.
        """
        try:
            current = node.parent()
            while current is not None:
                if is_filecache_node(current):
                    return True
                current = current.parent()
        except Exception:
            return False
        return False

    def filecache_owner(node):
        """Nearest File Cache HDA at or above node, or None."""
        current = node
        for _ in range(32):
            if current is None:
                return None
            if is_filecache_node(current):
                return current
            try:
                current = current.parent()
            except Exception:
                return None
        return None

    def sibling_unexpanded(node, parm_name):
        """Unexpanded value of a sibling parameter, or "" when absent."""
        try:
            sibling = node.parm(parm_name)
        except Exception:
            return ""
        if sibling is None:
            return ""
        try:
            return sibling.unexpandedString() or ""
        except Exception:
            try:
                return sibling.evalAsString() or ""
            except Exception:
                return ""

    def has_frame_token(text):
        """True when text still describes a sequence by itself.

        A bare indirection such as ``chs("../sopoutput")`` is not evidence: it
        says "read another parameter", not "this is a frame sequence". Only a
        value that still carries a token next to a real name qualifies.
        """
        text = str(text or "")
        if not SEQUENCE_TOKEN.search(text):
            return False
        return not ("chs(" in text or "ch(" in text or text.lstrip().startswith(("{", "return ")))

    def evidence_matches_leaf(text, leaf):
        """True when a token-carrying template can name exactly this leaf.

        A cache label is full of things Python cannot resolve —
        ``opname("..")`` sits inside backticks and is evaluated by HScript, not
        by ``evalAsString()`` — so every unknown part becomes a wildcard and
        only the literal segments plus the frame width have to agree. That is
        enough to tell "this frame of the cache" from "one frame of some other
        sequence that happens to live in the same folder": the latter has a
        different literal body and must never inherit the cache's token.
        """
        template = os.path.basename(str(text or "").replace("\\", "/").strip())
        target = os.path.basename(str(leaf or "").replace("\\", "/").strip())
        if not template or not target:
            return False
        parts = ["^"]
        index = 0
        for match in TEMPLATE_PART.finditer(template):
            parts.append(re.escape(template[index:match.start()]))
            token = match.group(0)
            width = FRAME_WIDTH.match(token)
            if width is not None:
                digits = width.group(1)
                parts.append(r"\d{%d}" % int(digits) if digits else r"\d+")
            else:
                parts.append(".*")
            index = match.end()
        parts.append(re.escape(template[index:]))
        parts.append("$")
        try:
            return re.match("".join(parts), target) is not None
        except re.error:
            return False

    def cache_frame_span(node):
        """(start, end) of a File Cache's frame range, or None when unknown."""
        values = []
        for name in ("f1", "f2"):
            try:
                parm = node.parm(name)
            except Exception:
                return None
            if parm is None:
                return None
            try:
                values.append(float(parm.eval()))
            except Exception:
                return None
        return values[0], values[1]

    def sequence_evidence(parm, raw=""):
        """(template, from_owner) pairs that can still prove a sequence.

        File Cache 2.0 builds ``sopoutput`` from ``basedir`` plus
        ``descriptivelabel``; what hou.fileReferences() hands over is the
        *resolved* value, so the frame token is already gone by the time the
        packager sees it. The token still lives on the HDA's own parameters,
        which is what makes recovery possible at all. Evidence taken from the
        HDA is flagged so it can be held to a stricter standard than a token
        the parameter spells out itself.
        """
        pairs = []
        if has_frame_token(raw):
            pairs.append((str(raw), False))
        try:
            unexpanded = parm.unexpandedString() or ""
        except Exception:
            unexpanded = ""
        if has_frame_token(unexpanded):
            pairs.append((unexpanded, False))
        owner = filecache_owner(parm.node())
        if owner is not None:
            for name in ("descriptivelabel", "sopoutput", "filepattern", "file"):
                text = sibling_unexpanded(owner, name)
                if has_frame_token(text):
                    pairs.append((text, True))
        return pairs

    def infer_sequence(parm, expanded):
        """(glob pattern, archive leaf) rebuilt from a single-frame path.

        Only called when the parameter lost its token, and only trusted when
        some parameter still shows one — a plain static reference must never
        be widened into a sequence (that would sweep unrelated frames, or
        worse, glob a whole directory). Frame files also have to differ by a
        same-width number, so a neighbouring asset cannot sneak in.

        Two further guards keep the widening honest: a token borrowed from the
        HDA must describe *this* file name (otherwise a one-frame reference to
        another sequence in the same folder would drag in the whole folder),
        and an HDA whose frame range is a single frame is cached as a still on
        purpose, so its neighbours stay out.
        """
        directory, name = os.path.split(expanded)
        match = FRAME_DIGITS_LEAF.match(name)
        if not directory or not match or not os.path.isfile(expanded):
            return "", ""
        evidence = sequence_evidence(parm)
        if not evidence:
            return "", ""
        owner = filecache_owner(parm.node())
        span = cache_frame_span(owner) if owner is not None else None
        single_frame = span is not None and abs(span[0] - span[1]) < 1e-6
        trusted = [
            text
            for text, from_owner in evidence
            if not from_owner or (evidence_matches_leaf(text, name) and not single_frame)
        ]
        if not trusted:
            return "", ""
        prefix, digits, suffix = match.groups()
        pattern = glob.escape(os.path.join(directory, prefix)) + "*" + glob.escape(suffix)
        frame_leaf = re.compile(re.escape(prefix) + r"\d{%d}" % len(digits) + re.escape(suffix) + "$")
        frames = [path for path in glob.glob(pattern) if os.path.isfile(path) and frame_leaf.match(os.path.basename(path))]
        if len(frames) < 2:
            return "", ""
        token = "$F%d" % len(digits) if len(digits) > 1 else "$F"
        return pattern, prefix + token + suffix

    def parm_is_computed(parm):
        """True when the value is produced by an expression, keys or a reference.

        File Cache 2.0's output parameters are all three at once: the asset is
        locked, ``sopoutput`` holds an HScript expression and ``read_back``
        holds a channel reference to it. ``Parm.set()`` returns happily for
        them without changing what Houdini evaluates.
        """
        try:
            if parm.keyframes():
                return True
        except Exception:
            pass
        try:
            if parm.expression():
                return True
        except Exception:
            pass
        try:
            referenced = parm.getReferencedParm()
            if referenced is not None and referenced.path() != parm.path():
                return True
        except Exception:
            pass
        return False

    def computed_path_is_relocatable(parm, expanded):
        """True when an untouched computed path still lands inside the package.

        A channel reference is fine: the parameter it points at is handled on
        its own. An expression is fine only when it is built from a $HIP/$JOB
        base — then saving into the package relocates it for free.
        """
        if not inside(expanded):
            return False
        try:
            referenced = parm.getReferencedParm()
        except Exception:
            referenced = None
        if referenced is not None and referenced.path() != parm.path():
            return True
        owner = filecache_owner(parm.node())
        if owner is None:
            return False
        for name in ("basedir", "cachedir"):
            base = sibling_unexpanded(owner, name)
            if "$HIP" in base or "$JOB" in base:
                return True
        return False

    def rewrite_parm(parm, rel_value, expected, expanded):
        """Rewrite a reference and report what actually happened.

        Computed parameters are never touched: writing them either does
        nothing or appends a keyframe on top of an expression, while the
        manifest claimed a rewrite that never took effect. When the untouched
        expression is $HIP-based it already resolves to the archived copy, so
        leaving it alone is the correct answer; otherwise the run fails rather
        than shipping a path that points back at the source.
        """
        if parm_is_computed(parm):
            if computed_path_is_relocatable(parm, expanded):
                return "skipped", ""
            return "failed", "参数由表达式/通道引用驱动，无法改写成 $HIP 路径，归档后仍指向原位置"
        try:
            parm.set(rel_value)
        except Exception as exc:
            return "failed", str(exc)
        try:
            actual = parm.evalAsString()
        except Exception:
            return "rewritten", ""
        if norm(expand(actual)) == norm(expected):
            return "rewritten", ""
        return "failed", "改写后参数仍解析到：%s" % actual

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
        elif skip_cache_outputs and node_type == "file" and parm_name == "file" and inside_filecache(node):
            # File Cache 2.0's read_back is a plain File SOP holding
            # `chs("../sopoutput")`: it reads the cache the HDA writes, so
            # keeping it would archive half a sequence plus one stale frame.
            # Only that reader is affected — the HDA's topnet / scheduler /
            # render parameters keep their own auxiliary and project-dir
            # handling.
            return "cache_output"
        cache_node = (not filecache_node) and any(x in node_type for x in ("filecache", "rop_geometry", "geometrycache", "dopimport", "alembiccache"))
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
    # Record which build actually opened the scene. Loading across versions is
    # allowed (the GUI already warned), but the manifest must stay honest about
    # it so a packaged result is never mistaken for a same-version round trip.
    try:
        opened_with = hou.applicationVersionString()
    except Exception:
        opened_with = ""
    if expect_version and opened_with and expect_version != opened_with:
        emit("status", message="版本不一致：保存于 %s，当前用 %s 打开。" % (expect_version, opened_with))
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
    root_node = hou.node("/")
    if root_node is not None:
        for node in root_node.allSubChildren():
            node_type = node.type().name().lower()
            if not node_type.startswith("filecache"):
                continue
            output_parm = node.parm("sopoutput")
            if output_parm is None or output_parm.path() in ref_parm_paths:
                continue
            # Read the unexpanded string so a `$F4` cache pattern stays a
            # sequence. evalAsString() would bake in the current frame and the
            # package would lose every other frame of the cache. Sopoutput is
            # often an expression whose unexpanded text is the expression
            # source, so only a candidate that still looks like a path is used.
            candidates = []
            for reader in ("unexpandedString", "rawValue", "evalAsString"):
                try:
                    candidates.append(getattr(output_parm, reader)() or "")
                except Exception:
                    candidates.append("")
            output_value = next(
                (value for value in candidates if looks_like_path(value)),
                next((value for value in candidates if value), ""),
            )
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
    copied = rewritten = rewrites_skipped = 0
    reused = 0
    filtered_cache = filtered_render = filtered_renderer = 0
    filtered_category = filtered_node = filtered_project_dirs = filtered_auxiliary = 0
    alembic_dir_refs = []
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
        if skip_auxiliary_files and not os.path.isdir(expanded) and is_auxiliary_file(expanded):
            item_meta["filtered_by"] = "auxiliary_file"
            manifest.append(item_meta)
            filtered_auxiliary += 1
            emit("status", message="跳过脚本/配置文件：%s" % (parm.path() if parm else expanded))
            continue
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
        if expanded.startswith(("temp:", "op:", "opdef:", "oplib:")):
            # Houdini internal node/temporary references are not real file
            # assets and should never be copied into the archive.
            continue
        if not os.path.isabs(expanded):
            # A path that is still not absolute after evaluation can be either
            # a project-relative reference (fine to skip) or an environment
            # variable that expandString() failed to resolve (a real gap in
            # the archive). Flag the unresolved variable so it never slips
            # through silently.
            if "$" in expanded or "%" in expanded:
                unresolved_item = dict(item_meta)
                unresolved_item["kind"] = "unresolved_path"
                manifest.append(unresolved_item)
                failures.append({"node": node_path, "parameter": parm.path() if parm else None, "resource": raw, "target": None, "reason": "unresolved_path"})
                emit("status", message="路径包含未解析变量，无法归档：%s" % raw)
            continue
        if builtin(expanded):
            builtin_item = dict(item_meta)
            builtin_item["kind"] = "houdini_builtin"
            manifest.append(builtin_item)
            continue

        # Handle Houdini sequences before the concrete-current-value check.
        # evalAsString() resolves `$F` and backtick expressions to a single
        # frame, but the parameter still describes a sequence, so every frame
        # has to be archived and the expression kept in the rewritten value.
        # Detection therefore runs on the raw string, never on `expanded`.
        pattern = sequence_pattern(raw)
        archive_name = sequence_basename(raw, expanded)
        inferred = False
        if not pattern and parm is not None:
            # The frame token can be eaten before the packager sees it: File
            # Cache 2.0's sopoutput resolves basedir + descriptivelabel to the
            # current frame, and its read_back node only holds
            # `chs("../sopoutput")`. Both then arrive as one concrete file.
            pattern, inferred_name = infer_sequence(parm, expanded)
            if pattern:
                archive_name = inferred_name
                inferred = True
        matches = sorted(m for m in glob.glob(pattern) if os.path.isfile(m)) if pattern else []
        if matches and parm is not None:
            sequence_meta = storage_info(os.path.dirname(expanded), category, directory_hint=True)
            rel_dir = sequence_meta["target"] or "."
            if inferred:
                emit("status", message="缓存输出已被解析为单帧，按帧号推断出序列：%s（%d 个文件）%s" % (rel_dir, len(matches), archive_name))
            emit("status", message="复制序列：%s（%d 个文件）%s" % (rel_dir, len(matches), archive_name))
            last_report = time.monotonic()
            try:
                for index, match in enumerate(matches, 1):
                    frame_copied, frame_reused = copy_item(match, os.path.join(package_dir, rel_dir, os.path.basename(match)))
                    copied += frame_copied
                    reused += frame_reused
                    # A long sequence over a network share can take minutes;
                    # report progress so the GUI never looks frozen.
                    now = time.monotonic()
                    if now - last_report >= 2.0:
                        last_report = now
                        emit("status", message="复制序列 %d/%d：%s" % (index, len(matches), os.path.basename(match)))
            except Exception as exc:
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel_dir, "reason": "copy_failed", "detail": str(exc)})
                emit("status", message="复制失败：%s" % raw)
                continue
            if not all(os.path.exists(os.path.join(package_dir, rel_dir, os.path.basename(match))) for match in matches):
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel_dir, "reason": "target_missing"})
                continue
            rel_value = hip_path(rel_dir, archive_name)
            status, detail = rewrite_parm(parm, rel_value, expand(rel_value), expanded)
            if status == "failed":
                failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel_dir, "reason": "rewrite_failed", "detail": detail})
                emit("status", message="改写参数失败：%s | %s" % (parm.path() if parm else raw, detail))
                continue
            if status == "skipped":
                item_meta["rewrite_skipped"] = "computed_parameter"
                rewrites_skipped += 1
                emit("status", message="参数由表达式驱动，保留原值（仍解析到归档副本）：%s" % (parm.path() if parm else raw))
            else:
                rewritten += 1
            verified += 1
            item = dict(item_meta)
            item.update(sequence_meta)
            item.update({"target": rel_dir, "sequence_matches": len(matches), "sequence_pattern": pattern})
            if status == "rewritten":
                item["rewritten"] = rel_value
            if inferred:
                # Visible in the manifest so a recovered sequence is never
                # mistaken for one the scene still spelled out explicitly.
                item["sequence_inferred"] = True
            manifest.append(item)
            continue

        if os.path.exists(expanded):
            if os.path.isdir(expanded) and covers_source_root(expanded):
                # Path parameters point at the project folder more often than
                # expected (PDG working directories, $JOB style roots).
                # Copying such a folder would drag every other scene, backup
                # and cache in the project into the package, so record the
                # reference, keep its value untouched and skip the copy.
                root_item = dict(item_meta)
                root_item["kind"] = "project_directory_skipped"
                manifest.append(root_item)
                filtered_project_dirs += 1
                emit("status", message="跳过工程目录引用，不复制整个工程：%s" % (parm.path() if parm else expanded))
                continue
            rel = storage_meta["target"]
            if os.path.isdir(expanded) and category == "Alembics":
                # An Alembic folder parameter (a python node root, for
                # example) used to pull in every .abc stored beside the
                # referenced file. Only individually referenced files are
                # copied now, so point the folder parameter at the rebuilt
                # folder and leave the copy to those file references.
                directory_item = dict(item_meta)
                directory_item["kind"] = "alembic_directory_reference"
                manifest.append(directory_item)
                alembic_dir_refs.append((rel, directory_item))
                if parm is not None and (os.path.isabs(raw) or expanded != raw):
                    hip_dir = "$HIP/" + (rel or ".").lstrip("/")
                    try:
                        parm.set(hip_dir)
                    except Exception as exc:
                        failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel, "reason": "rewrite_failed", "detail": str(exc)})
                        continue
                    rewritten += 1
                    directory_item["rewritten"] = hip_dir
                emit("status", message="Alembic 目录引用按需重建，不复制整个文件夹：%s -> %s" % (parm.path() if parm else expanded, rel))
                continue
            if storage_meta.get("source_is_external") and category:
                emit("status", message="外部资源分类：%s | 归档目录：%s | 复制资源：%s" % (category, storage_meta.get("storage_directory"), os.path.basename(expanded)))
            elif storage_meta.get("source_is_external"):
                emit("status", message="未识别标准类型，归档到 external | 复制资源：%s" % os.path.basename(expanded))
            else:
                emit("status", message="复制资源：%s -> %s" % (expanded, rel))
            target_path = os.path.join(package_dir, rel.replace("/", os.sep))
            copy_source = expanded
            copy_target = target_path
            # Only the referenced file is copied; copy_item() builds whatever
            # folder depth the target needs, so sibling Alembic files from the
            # same source folder stay out of the package.
            folder_copy_key = (norm(copy_source), norm(copy_target)) if os.path.isdir(copy_source) else None
            folder_reused = folder_copy_key in copied_folder_targets if folder_copy_key else False
            try:
                if folder_reused:
                    copied_now = reused_now = 0
                else:
                    copied_now, reused_now = copy_item(copy_source, copy_target)
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
            reused += reused_now
            item = dict(item_meta)
            item["target"] = rel
            if parm is not None and (os.path.isabs(raw) or expanded != raw):
                if not (inside(expanded) and ("$HIP" in raw or "$JOB" in raw)):
                    hip_rel = "$HIP/" + rel.lstrip("/")
                    status, detail = rewrite_parm(parm, hip_rel, expand(hip_rel), expanded)
                    if status == "failed":
                        failures.append({"node": parm.node().path() if parm else None, "parameter": parm.path() if parm else None, "resource": raw, "target": rel, "reason": "rewrite_failed", "detail": detail})
                        emit("status", message="改写参数失败：%s | %s" % (parm.path() if parm else raw, detail))
                        continue
                    if status == "skipped":
                        item["rewrite_skipped"] = "computed_parameter"
                        rewrites_skipped += 1
                        emit("status", message="参数由表达式驱动，保留原值（仍解析到归档副本）：%s" % (parm.path() if parm else raw))
                    else:
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

    empty_alembic_dirs = 0
    for rel_dir, directory_item in alembic_dir_refs:
        rebuilt = os.path.join(package_dir, (rel_dir or ".").replace("/", os.sep))
        has_files = False
        if os.path.isdir(rebuilt):
            for _, _, file_names in os.walk(rebuilt):
                if file_names:
                    has_files = True
                    break
        if not has_files:
            empty_alembic_dirs += 1
            directory_item["empty_after_rebuild"] = True
            failures.append({"node": None, "parameter": directory_item.get("parm"), "resource": directory_item.get("raw"), "target": rel_dir, "reason": "empty_alembic_directory"})
            emit("status", message="Alembic 目录引用没有对应的文件级引用，重建后为空：%s" % rel_dir)

    # One physical file can still be archived twice when the two references do
    # not share a destination: a static path plus a sequence pointing at the
    # same frame, a raw expression written two ways, or the same file reached
    # through a mapped drive and a real path. Both copies are required (each
    # parameter is rewritten to its own $HIP path), so this is reported rather
    # than silently deduplicated.
    duplicate_groups = []
    for entry in archived_sources.values():
        if len(entry["targets"]) > 1:
            targets = sorted(entry["targets"])
            duplicate_groups.append({"source": entry["source"], "targets": targets})
            emit("status", message="同一资源归档到多个位置，会占两份空间：%s -> %s" % (entry["source"], "，".join(targets)))

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
        "houdini": {
            "saved_with": expect_version or None,
            "opened_with": opened_with or None,
            "version_match": bool(expect_version and opened_with and expect_version == opened_with),
            "minor_match": major_minor(expect_version) == major_minor(opened_with) if (expect_version and opened_with) else None,
        },
        "summary": {
            "references_seen": len(refs),
            "resources_copied": copied,
            "references_reused": reused,
            "duplicate_archive": len(duplicate_groups),
            "parameters_rewritten": rewritten,
            "rewrites_skipped": rewrites_skipped,
            "verified": verified,
            "failed": len(failures),
            "filtered": {
                "cache_outputs": filtered_cache,
                "render_outputs": filtered_render,
                "renderer_nodes": filtered_renderer,
                "category": filtered_category,
                "node_rule": filtered_node,
                "project_dirs": filtered_project_dirs,
                "auxiliary_files": filtered_auxiliary + folder_auxiliary_skipped,
            },
            "category_counts": category_counts,
            "not_found": len(missing),
            "alembic_dirs_empty": empty_alembic_dirs,
        },
        "resources": manifest,
        "duplicates": duplicate_groups,
        "failures": failures,
    }
    archive_log_path = os.path.join(package_dir, "archive_log.txt")
    archive_log_temp_path = archive_log_path + ".tmp"
    with open(archive_log_temp_path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(build_archive_log(data))
        f.flush()
        os.fsync(f.fileno())
    os.replace(archive_log_temp_path, archive_log_path)
    manifest_temp_path = manifest_path + ".tmp"
    with open(manifest_temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(manifest_temp_path, manifest_path)
    emit("done", package=dest, manifest=manifest_path, status=status, failures=len(failures), resources_copied=copied, resources_reused=reused, duplicate_archive=len(duplicate_groups), parameters_rewritten=rewritten, filtered_cache_outputs=filtered_cache, filtered_render_outputs=filtered_render, filtered_renderer_nodes=filtered_renderer, filtered_category=filtered_category, filtered_node_rule=filtered_node, filtered_project_dirs=filtered_project_dirs, filtered_auxiliary_files=filtered_auxiliary + folder_auxiliary_skipped, category_counts=category_counts, not_found=len(missing))


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
    expect_version = ""
    if "--expect-version" in options:
        pos = options.index("--expect-version")
        if pos + 1 < len(options):
            expect_version = options[pos + 1]
    package(sys.argv[1], sys.argv[2], "--include-cache-outputs" not in options, "--include-render-outputs" not in options, categories, "--filter-red-nodes" in options, "--filter-external" in options, fixed_package_dir=fixed_package_dir, skip_auxiliary_files="--include-auxiliary-files" not in options, expect_version=expect_version)
