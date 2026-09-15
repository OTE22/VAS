#!/usr/bin/env python3
"""Print a non-secret migration inventory; does not export databases or data."""
import datetime
import json
import subprocess


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True)


names = [name for name in docker("ps", "-a", "--format", "{{.Names}}").splitlines()
         if name.startswith("face_detector_prod-") or
         name in {"VMS", "VMS-db", "vas-assistant", "vas-assistant-gate"}]
report = {"captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "containers": [], "images_to_export": []}
images = set()
for name in sorted(names):
    container = json.loads(docker("inspect", name))[0]
    image = json.loads(docker("image", "inspect", container["Image"]))[0]
    images.add(container["Config"]["Image"])
    report["containers"].append({
        "name": name, "image": container["Config"]["Image"], "image_id": container["Image"],
        "architecture": image["Architecture"], "os": image["Os"],
        "state": container["State"]["Status"],
        "restart_policy": container["HostConfig"]["RestartPolicy"]["Name"],
        "mounts": [{key: mount[key] for key in ("Type", "Name", "Source", "Destination", "RW")
                    if key in mount} for mount in container["Mounts"]],
        "networks": sorted(container["NetworkSettings"]["Networks"]),
    })
report["images_to_export"] = sorted(images)
print(json.dumps(report, indent=2))
