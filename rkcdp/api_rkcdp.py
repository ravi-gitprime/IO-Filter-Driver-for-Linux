# SPDX-License-Identifier: GPL-2.0
"""
rkcdp endpoints for the KVMDR manager API.

Hook into api.py (after `app`, get_db and require_admin exist):

    import api_rkcdp
    api_rkcdp.init(app, get_db=get_db, require_admin=require_admin)

Endpoints:
    GET  /api/rkcdp/nodes                 per-node protection + replica state
    POST /api/rkcdp/rebuild               {node, host_id, vmid?, storage?, bridge?, force?}
    GET  /api/rkcdp/rebuild/{node}        progress of a running/finished rebuild
"""
import json
import os
import subprocess
import time

JOURNAL = os.environ.get("RKCDP_JOURNAL", "/replication/_kvmdr/rkcdp")
REBUILD_BIN = os.environ.get("RKCDP_REBUILD", "/opt/rkcdp/rkcdp-rebuild.py")
PROGRESS_DIR = "/run/rkcdp"
STALE_SEC = 60


def _load(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def _nodes():
    out = []
    if not os.path.isdir(JOURNAL):
        return out
    for name in sorted(os.listdir(JOURNAL)):
        d = os.path.join(JOURNAL, name)
        if not os.path.isdir(d) or name == "rebuild" or name.startswith("."):
            continue
        manifest = _load(os.path.join(d, "manifest.json"), {})
        status = _load(os.path.join(d, "status.json"), {})
        applied = _load(os.path.join(d, "applied.json"), {})
        node = _load(os.path.join(d, "node.json"), {})
        now = time.time()
        age = now - status.get("time", 0) if status else None
        if age is not None and age <= STALE_SEC and status.get("state") == "SYNCING":
            state = "syncing"
        elif "base_end_seq" not in manifest:    # 0 is a valid value
            state = "unprotected"
        elif age is None or age > STALE_SEC:
            state = "stale"
        elif status.get("state") == "BITMAP":
            state = "bitmap"
        else:
            state = "cdp"
        pending = 0
        cyc = os.path.join(d, "cycles")
        if os.path.isdir(cyc):
            pending = len([n for n in os.listdir(cyc) if n.endswith(".bin")])
        replica = os.path.join(d, "replica.raw")
        rstat = os.stat(replica) if os.path.exists(replica) else None
        prog = _load(os.path.join(PROGRESS_DIR, "%s.json" % name))
        out.append({
            "node": name,
            "state": state,
            "status_age_sec": None if age is None else int(age),
            "last_seq": status.get("last_seq"),
            "seq_next": status.get("seq_next"),
            "overflows": status.get("overflows"),
            "applied_seq": applied.get("last_seq", manifest.get("base_end_seq")),
            "applied_time": applied.get("last_time") or manifest.get("base_time"),
            "pending_cycles": pending,
            "replica_bytes": rstat.st_size if rstat else None,
            "replica_used_bytes": rstat.st_blocks * 512 if rstat else None,
            "device": manifest.get("device"),
            "ips": node.get("ips"), "mem_mb": node.get("mem_mb"), "cpus": node.get("cpus"),
            "base_copy": status.get("base_copy"),
            "rebuilding": bool(prog and prog.get("state") == "running"),
            "rebuild": prog,
        })
    return out


def init(app, get_db, require_admin):
    from fastapi import Request, HTTPException

    @app.get("/api/rkcdp/nodes")
    def rkcdp_nodes(request: Request):
        require_admin(request)
        return _nodes()

    @app.get("/api/rkcdp/rebuild/{node}")
    def rkcdp_rebuild_status(node: str, request: Request):
        require_admin(request)
        return _load(os.path.join(PROGRESS_DIR, "%s.json" % node)) or {"state": "none"}

    @app.post("/api/rkcdp/rebuild")
    def rkcdp_rebuild(payload: dict, request: Request):
        u = require_admin(request)
        node = (payload.get("node") or "").strip()
        host_id = payload.get("host_id")
        if not node or host_id is None:
            raise HTTPException(400, "node and host_id required")
        if "/" in node or node.startswith("."):
            raise HTTPException(400, "bad node name")
        prog = _load(os.path.join(PROGRESS_DIR, "%s.json" % node))
        if prog and prog.get("state") == "running":
            raise HTTPException(409, "rebuild already running for %s" % node)

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT hostname, ip, ssh_user, hv_type FROM hosts WHERE host_id = %s", (host_id,))
                h = cur.fetchone()
        finally:
            conn.close()
        if not h:
            raise HTTPException(404, "host not found")
        h = dict(h)
        if (h.get("hv_type") or "").lower() not in ("proxmox", "pve", ""):
            raise HTTPException(400, "rebuild currently supports Proxmox hosts only")

        os.makedirs(PROGRESS_DIR, exist_ok=True)
        pfile = os.path.join(PROGRESS_DIR, "%s.json" % node)
        cmd = ["python3", REBUILD_BIN, "--node", node, "--host", h["ip"],
               "--user", h.get("ssh_user") or "root", "--journal", JOURNAL,
               "--progress", pfile]
        if payload.get("vmid"):
            cmd += ["--vmid", str(int(payload["vmid"]))]
        if payload.get("storage"):
            cmd += ["--storage", payload["storage"]]
        if payload.get("bridge"):
            cmd += ["--bridge", payload["bridge"]]
        if payload.get("force"):
            cmd += ["--force"]
        log = open("/var/log/kvmdr/rkcdp-rebuild-%s.log" % node, "a") if os.path.isdir("/var/log/kvmdr") else subprocess.DEVNULL
        subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        return {"ok": True, "node": node, "host": h["hostname"], "started_by": u.get("username")}
