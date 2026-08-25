import os
import sys
import hou

hip = os.path.abspath(sys.argv[1])
package_dir = os.path.dirname(hip)
changed = 0
hou.hipFile.load(hip, suppress_save_prompt=True, ignore_load_warnings=True)
for parm, raw in hou.fileReferences():
    if parm is None or not isinstance(raw, str) or raw.startswith("$HIP/"):
        continue
    value = raw.replace("\\", "/")
    if value.startswith(("external/", "abc/", "geo/", "render/", "galleries/")):
        parm.set("$HIP/" + value)
        changed += 1
dest = hip
hou.hipFile.save(dest, save_to_recent_files=False)
print("repaired", changed, dest)
