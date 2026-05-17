"""
Incus service — all subprocess calls.

FIX #1: security.privileged=true + security.nesting=true added to container
        creation, fixing "Failed to mount proc – Operation not permitted"
FIX #6: every _run() call logs the command and stderr on failure
FIX #7: argument-list subprocess (no shell=True), protocol/image whitelisting
"""
import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

AVAILABLE_IMAGES = [
    "ubuntu/22.04", "ubuntu/20.04", "ubuntu/24.04",
    "debian/12", "debian/11",
    "alpine/3.19",
    "centos/9-Stream",
    "fedora/39",
    "archlinux",
]

BLOCKED_PATTERNS = [
    "rm -rf /", "mkfs", "dd if=/dev/zero", "dd if=/dev/random",
    "> /dev/sda", ":(){ :|:& };:", "chmod 777 /",
    "shutdown", "reboot", "halt", "poweroff",
]


class IncusService:

    CREATE_TIMEOUT = 180
    DEFAULT_TIMEOUT = 60

    @staticmethod
    def _run(args: list, timeout: int = 60) -> dict:
        cmd_str = " ".join(str(a) for a in args)
        logger.debug("incus: %s", cmd_str)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                logger.warning("incus command failed (rc=%d): %s\n  stderr: %s",
                               r.returncode, cmd_str, r.stderr.strip())
            return {"success": r.returncode == 0, "output": r.stdout,
                    "error": r.stderr, "returncode": r.returncode}
        except subprocess.TimeoutExpired:
            logger.error("incus timed out (%ds): %s", timeout, cmd_str)
            return {"success": False, "output": "", "error": "Command timed out", "returncode": -1}
        except FileNotFoundError:
            logger.error("incus binary not found in PATH")
            return {"success": False, "output": "", "error": "incus not found in PATH", "returncode": -1}
        except Exception as exc:
            logger.exception("incus unexpected error: %s", exc)
            return {"success": False, "output": "", "error": str(exc), "returncode": -1}

    @classmethod
    def _target(cls, name: str, remote: Optional[str]) -> str:
        return f"{remote}:{name}" if remote else name

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, name: str, ram: int, cpu: int, disk_gb: int = 20,
               os_image: str = "ubuntu/22.04", remote: Optional[str] = None,
               privileged: bool = True, is_vm: bool = False) -> dict:
        """
        Create an Incus container or VM with correct disk size from first boot.

        Flow:
          1. incus init   — create stopped instance (no profile disk default applied yet)
          2. incus config device override root size=<disk_gb>GB  — override disk BEFORE boot
          3. incus start  — boot with correct disk already in place

        'incus launch', 'device add', and 'device set' are NOT used.
        'device override' is the correct Incus command for overriding profile-inherited
        devices (like the default root disk) on a per-instance basis before first boot.
        """
        if os_image not in AVAILABLE_IMAGES:
            os_image = "ubuntu/22.04"
        target = cls._target(name, remote)

        # ── Step 1: incus init — create stopped instance ──────────────────────
        init_args = [
            "incus", "init", f"images:{os_image}", target,
            "--config", f"limits.memory={ram}MB",
            "--config", f"limits.cpu={cpu}",
        ]
        if is_vm:
            init_args.append("--vm")
            logger.info("Initialising VM %s (ram=%dMB cpu=%d disk=%dGB)",
                        target, ram, cpu, disk_gb)
        else:
            if privileged:
                init_args += [
                    "--config", "security.privileged=true",
                    "--config", "security.nesting=true",
                ]
            logger.info("Initialising container %s (ram=%dMB cpu=%d disk=%dGB privileged=%s)",
                        target, ram, cpu, disk_gb, privileged)

        init_result = cls._run(init_args, timeout=cls.CREATE_TIMEOUT)
        if not init_result["success"]:
            logger.error("incus init failed for %s: %s", name, init_result["error"].strip())
            return init_result

        # ── Step 2: device override — set disk size BEFORE first boot ─────────
        # 'device override' creates a per-instance override of the profile's root
        # disk device, which is the only reliable way to change root disk size.
        override_result = cls._run([
            "incus", "config", "device", "override", target,
            "root", f"size={disk_gb}GB",
        ])
        if not override_result["success"]:
            logger.warning(
                "Disk override failed for %s (will use profile default): %s",
                name, override_result["error"].strip(),
            )

        # ── Step 3: incus start — boot with correct disk size ─────────────────
        start_result = cls._run(
            ["incus", "start", target],
            timeout=cls.CREATE_TIMEOUT,
        )
        if not start_result["success"]:
            logger.error("incus start failed for %s: %s", name, start_result["error"].strip())
        return start_result

    @classmethod
    def start(cls, name: str, remote: Optional[str] = None) -> dict:
        return cls._run(["incus", "start", cls._target(name, remote)])

    @classmethod
    def stop(cls, name: str, remote: Optional[str] = None) -> dict:
        return cls._run(["incus", "stop", cls._target(name, remote), "--force"])

    @classmethod
    def restart(cls, name: str, remote: Optional[str] = None) -> dict:
        return cls._run(["incus", "restart", cls._target(name, remote), "--force"])

    @classmethod
    def delete(cls, name: str, remote: Optional[str] = None) -> dict:
        return cls._run(["incus", "delete", cls._target(name, remote), "--force"])

    @classmethod
    def rebuild(cls, name: str, ram: int, cpu: int, disk_gb: int,
                os_image: str = "ubuntu/22.04", remote: Optional[str] = None,
                is_vm: bool = False) -> dict:
        """Delete then recreate the instance (container or VM)."""
        logger.info("Rebuilding %s with %s (vm=%s)", name, os_image, is_vm)
        cls.delete(name, remote)
        return cls.create(name, ram, cpu, disk_gb, os_image, remote,
                          privileged=not is_vm, is_vm=is_vm)

    @classmethod
    def config_set(cls, name: str, key: str, value: str,
                   remote: Optional[str] = None) -> dict:
        """Apply a single `incus config set NAME KEY=VALUE`."""
        target = cls._target(name, remote)
        logger.info("config set %s %s=%s", target, key, value)
        return cls._run(["incus", "config", "set", target, f"{key}={value}"])

    @classmethod
    def sync_node(cls, remote: Optional[str] = None) -> set:
        """
        Return the set of instance names that exist on the node.
        Used for ghost-record cleanup: compare with the DB and delete orphans.
        """
        return {item["name"] for item in cls.list_vps(remote=remote)}


    @classmethod
    def list_vps(cls, remote: Optional[str] = None) -> list:
        prefix = f"{remote}:" if remote else ""
        result = cls._run(["incus", "list", prefix, "--format", "json"])
        if not result["success"]:
            return []
        try:
            return [
                {
                    "name":   item["name"],
                    "status": item.get("status", "UNKNOWN").lower(),
                    "ram":    item.get("config", {}).get("limits.memory", "?"),
                    "cpu":    item.get("config", {}).get("limits.cpu", "?"),
                    "ipv4":   cls._extract_ip(item, "inet"),
                    "ipv6":   cls._extract_ip(item, "inet6"),
                }
                for item in json.loads(result["output"])
            ]
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("list_vps parse error: %s", exc)
            return []

    @classmethod
    def _extract_ip(cls, item: dict, family: str = "inet") -> str:
        try:
            for iface, net in item.get("state", {}).get("network", {}).items():
                if iface == "lo":
                    continue
                for addr in net.get("addresses", []):
                    if addr.get("family") == family:
                        a = addr.get("address", "")
                        if a and a not in ("127.0.0.1", "::1") and not a.startswith("fe80"):
                            return a
        except Exception:
            pass
        return ""

    @classmethod
    def get_info(cls, name: str, remote: Optional[str] = None) -> dict:
        r = cls._run(["incus", "info", cls._target(name, remote), "--format", "json"])
        if not r["success"]:
            return {}
        try:
            return json.loads(r["output"])
        except json.JSONDecodeError:
            return {}

    # ── Metrics ───────────────────────────────────────────────────────────────

    # Per-instance delta cache: cache_key → (monotonic_time, cpu_ns, net_rx_bytes, net_tx_bytes)
    _metrics_cache: dict = {}

    # Per-instance last-known-good result cache: cache_key → metrics dict
    # Served as fallback when Incus is slow or unreachable.
    # Never contains cpu (rate-based, meaningless when stale) — cpu is set to None on fallback.
    _last_good_cache: dict = {}

    # Hard cap on how long get_state() may block.  Must be strictly less than
    # any HTTP-level timeout the frontend applies so we always return before
    # the browser gives up.  3 seconds is safe for LAN-attached Incus.
    METRICS_TIMEOUT: int = 3

    @classmethod
    def get_state(cls, name: str, remote: Optional[str] = None,
                  timeout: int = 0) -> dict:
        """
        Fetch live container state using `incus query`.

        timeout: subprocess deadline in seconds.  Defaults to cls.METRICS_TIMEOUT
                 so callers that don't pass a timeout get the safe 3-second cap
                 automatically.  Pass an explicit value to override (e.g. the
                 worker background job can afford a longer window).

        This Incus version returns the state directly (no REST envelope):
          {
            "cpu":     {"usage": 12345678901},
            "memory":  {"usage": 1646833664},
            "network": {"eth0": {"counters": {"bytes_received": ..., "bytes_sent": ...}}},
            "disk":    {"root": {"read_bytes": ..., "write_bytes": ...}},
            "status":  "Running"
          }

        Some Incus versions wrap the state in a REST envelope:
          {"type": "sync", "status_code": 200, "metadata": { ...state... }}

        Both formats are handled.  Returns {} on any failure.
        """
        _timeout = timeout if timeout > 0 else cls.METRICS_TIMEOUT

        if remote:
            cmd = ["incus", "query", "--target", remote,
                   f"/1.0/instances/{name}/state"]
        else:
            cmd = ["incus", "query", f"/1.0/instances/{name}/state"]

        r = cls._run(cmd, timeout=_timeout)
        if not r["success"] or not r["output"].strip():
            logger.warning("get_state: incus query failed for %s@%s: %s",
                           name, remote or "local", r["error"].strip()[:200])
            return {}

        try:
            data = json.loads(r["output"])
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("get_state: JSON parse error for %s: %s", name, exc)
            return {}

        # If the response has a "metadata" key it's the REST envelope wrapper —
        # the real state is inside metadata.
        if "metadata" in data:
            state = data["metadata"]
        else:
            state = data

        if not isinstance(state, dict) or not state:
            logger.warning("get_state: empty state for %s@%s; keys=%s",
                           name, remote or "local", list(data.keys()))
            return {}

        logger.debug("get_state OK for %s@%s: cpu_ns=%s mem_bytes=%s",
                     name, remote or "local",
                     (state.get("cpu") or {}).get("usage"),
                     (state.get("memory") or {}).get("usage"))
        return state

    @classmethod
    def get_metrics(cls, name: str, remote: Optional[str] = None,
                    ram_limit_mb: int = 0) -> dict:
        """
        Return live container metrics dict.  NEVER raises — always returns valid JSON.

        Timeout: get_state() is capped at METRICS_TIMEOUT (3s) so this method
        returns in ≤ 3s even when Incus is under load or unreachable.

        Fallback priority on failure:
          1. _last_good_cache (last successful result, cpu set to null)
          2. Safe-zero defaults

        The caller can detect a stale/failed response via status='unavailable'.
        """
        import time
        cache_key = (remote or "") + ":" + name

        # ── Safe-zero defaults ─────────────────────────────────────────────────
        _empty = {
            "cpu": None, "ram_used": 0, "ram_total": ram_limit_mb, "ram_pct": 0,
            "net_rx": 0, "net_tx": 0, "net_rx_rate": 0, "net_tx_rate": 0,
            "disk_read": 0, "disk_write": 0,
            "status": "unavailable",
        }

        try:
            state = cls.get_state(name, remote)  # hard-capped at METRICS_TIMEOUT
        except Exception as exc:
            logger.warning(
                "metrics: get_state raised for vps=%s remote=%s: %s",
                name, remote or "local", exc,
            )
            state = {}

        if not state:
            # State unavailable — try last-known-good, else zeros
            last = cls._last_good_cache.get(cache_key)
            if last:
                fallback = dict(last)
                fallback["cpu"]    = None
                fallback["status"] = "unavailable"
                logger.warning(
                    "metrics: unavailable for vps=%s — returning last-known-good "
                    "(ram=%sMB status=unavailable)",
                    name, fallback.get("ram_used"),
                )
                return fallback
            logger.warning(
                "metrics: unavailable for vps=%s — no cache, returning zeros", name
            )
            return _empty

        try:
            # ── CPU — state["cpu"]["usage"] = cumulative nanoseconds ──────────
            cpu_block = state.get("cpu")
            cpu_ns = int(cpu_block.get("usage") or 0) if isinstance(cpu_block, dict) else 0

            # ── Memory — state["memory"]["usage"] = current bytes ────────────
            mem_block = state.get("memory")
            if not isinstance(mem_block, dict):
                mem_block = {}
            ram_used_bytes = int(mem_block.get("usage") or 0)
            ram_used_mb    = ram_used_bytes // (1024 * 1024)

            ram_total = ram_limit_mb
            if not ram_total:
                peak = int(mem_block.get("usage_peak") or 0)
                ram_total = peak // (1024 * 1024)

            # ── Network ───────────────────────────────────────────────────────
            net_rx_bytes = 0
            net_tx_bytes = 0
            network = state.get("network")
            if isinstance(network, dict):
                for iface, idata in network.items():
                    if iface in ("lo", "loopback"):
                        continue
                    if not isinstance(idata, dict):
                        continue
                    counters = idata.get("counters")
                    if not isinstance(counters, dict):
                        continue
                    net_rx_bytes += int(counters.get("bytes_received") or 0)
                    net_tx_bytes += int(counters.get("bytes_sent") or 0)

            # ── Disk I/O ──────────────────────────────────────────────────────
            disk_r_bytes = 0
            disk_w_bytes = 0
            disk = state.get("disk")
            if isinstance(disk, dict):
                for _dev, ddata in disk.items():
                    if not isinstance(ddata, dict):
                        continue
                    disk_r_bytes += int(ddata.get("read_bytes")  or 0)
                    disk_w_bytes += int(ddata.get("write_bytes") or 0)

            # ── Delta computation ─────────────────────────────────────────────
            now  = time.monotonic()
            prev = cls._metrics_cache.get(cache_key)

            cpu_pct     = 0.0
            net_rx_rate = 0.0
            net_tx_rate = 0.0

            if prev is not None:
                prev_time, prev_cpu_ns, prev_rx, prev_tx = prev
                dt = now - prev_time
                if dt > 0:
                    d_cpu = cpu_ns - prev_cpu_ns
                    if d_cpu > 0:
                        cpu_pct = round(min((d_cpu / (dt * 1_000_000_000)) * 100, 100.0), 1)
                    d_rx = max(0, net_rx_bytes - prev_rx)
                    d_tx = max(0, net_tx_bytes - prev_tx)
                    net_rx_rate = round(d_rx / dt / 1024, 1)
                    net_tx_rate = round(d_tx / dt / 1024, 1)

            cls._metrics_cache[cache_key] = (now, cpu_ns, net_rx_bytes, net_tx_bytes)

            ram_pct = round(ram_used_mb / ram_total * 100, 1) if ram_total > 0 else 0

            logger.debug(
                "metrics %s: cpu_ns=%d cpu=%.1f%% ram=%dMB/%dMB(%.0f%%) "
                "net_rx=%dKB net_tx=%dKB rx_rate=%.1f tx_rate=%.1f "
                "disk_r=%dKB disk_w=%dKB",
                name, cpu_ns, cpu_pct,
                ram_used_mb, ram_total, ram_pct,
                net_rx_bytes // 1024, net_tx_bytes // 1024,
                net_rx_rate, net_tx_rate,
                disk_r_bytes // 1024, disk_w_bytes // 1024,
            )

            result = {
                "cpu":         cpu_pct,
                "ram_used":    ram_used_mb,
                "ram_total":   ram_total,
                "ram_pct":     ram_pct,
                "net_rx":      net_rx_bytes // 1024,
                "net_tx":      net_tx_bytes // 1024,
                "net_rx_rate": net_rx_rate,
                "net_tx_rate": net_tx_rate,
                "disk_read":   disk_r_bytes // 1024,
                "disk_write":  disk_w_bytes // 1024,
                "status":      "ok",
            }

            # Update last-known-good so fallback has fresh data
            cls._last_good_cache[cache_key] = result
            return result

        except Exception as exc:
            logger.warning(
                "metrics: parse error for vps=%s remote=%s: %s",
                name, remote or "local", exc,
            )
            last = cls._last_good_cache.get(cache_key)
            if last:
                fallback = dict(last)
                fallback["cpu"]    = None
                fallback["status"] = "unavailable"
                return fallback
            return _empty


    # ── Node Info ─────────────────────────────────────────────────────────────

    @classmethod
    def get_node_info(cls, remote: Optional[str] = None) -> dict:
        empty = {"status": "offline", "cpu_load": 0.0, "online": False,
                 "ram_used_mb": 0, "ram_total_mb": 0,
                 "disk_used_gb": 0, "disk_total_gb": 0, "cpu_cores": 0}
        prefix = [f"{remote}:"] if remote else []

        # ── Try JSON first (Incus 0.5+) ───────────────────────────────────────
        rj = cls._run(["incus", "info"] + prefix + ["--format", "json"])
        if rj["success"]:
            try:
                data = json.loads(rj["output"])
                res  = data.get("resources", {})
                mem  = res.get("memory", {})
                cpu  = res.get("cpu", {})

                # Disk: sum all storage pools
                disk_used_gb  = 0
                disk_total_gb = 0
                for pool in res.get("storage", []):
                    disk_used_gb  += int((pool.get("used",  0) or 0) // (1024 ** 3))
                    disk_total_gb += int((pool.get("total", 0) or 0) // (1024 ** 3))

                result = {
                    "status":        "online",
                    "online":        True,
                    "cpu_cores":     int(cpu.get("total") or 0),
                    "cpu_load":      float(data.get("cpu_usage", 0) or 0),
                    "ram_total_mb":  int((mem.get("total") or 0) // (1024 * 1024)),
                    "ram_used_mb":   int((mem.get("used")  or 0) // (1024 * 1024)),
                    "disk_used_gb":  disk_used_gb,
                    "disk_total_gb": disk_total_gb,
                }

                # If cpu_load not in JSON, try reading /proc/loadavg via exec
                if result["cpu_load"] == 0.0 and result["cpu_cores"] > 0:
                    try:
                        load_args = ["incus", "exec"] + prefix[:-1] if prefix else ["incus", "exec"]
                        # exec requires an instance name — skip for server-level info
                        pass
                    except Exception:
                        pass

                logger.debug("get_node_info(JSON) remote=%r → %dMB/%dMB %d cores",
                             remote, result["ram_used_mb"], result["ram_total_mb"], result["cpu_cores"])
                return result

            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("get_node_info JSON parse error: %s", e)
                # fall through to text parsing

        # ── Text output fallback ──────────────────────────────────────────────
        r = cls._run(["incus", "info"] + prefix)
        if not r["success"]:
            logger.warning("get_node_info failed remote=%r: %s",
                           remote, r["error"].strip())
            return empty

        out  = {**empty, "status": "online", "online": True}
        text = r["output"]

        def _to_mb(val: str, unit: str) -> int:
            try:
                v = float(val)
                u = unit.upper().rstrip("B")   # GiB→GI, GB→G, MiB→MI, MB→M
                if u in ("GIB", "GI", "GB", "G"):
                    return int(v * 1024)
                return int(v)
            except (ValueError, TypeError):
                return 0

        def _to_gb(val: str, unit: str) -> int:
            try:
                v  = float(val)
                u  = unit.upper().rstrip("B")
                if u in ("TIB", "TI", "TB", "T"):
                    return int(v * 1024)
                if u in ("MIB", "MI", "MB", "M"):
                    return max(1, int(v / 1024))
                return int(v)
            except (ValueError, TypeError):
                return 0

        # RAM: "X GiB used of Y GiB" or "X MiB used of Y MiB"
        m = re.search(
            r"Memory\s*(?:\(RAM\))?:\s*([\d.]+)\s*(GiB|GB|MiB|MB)"
            r"\s+used\s+of\s+([\d.]+)\s*(GiB|GB|MiB|MB)",
            text, re.I)
        if m:
            out["ram_used_mb"]  = _to_mb(m.group(1), m.group(2))
            out["ram_total_mb"] = _to_mb(m.group(3), m.group(4))

        # CPU count
        m2 = re.search(r"CPUs?:\s+(\d+)", text, re.I)
        if m2:
            out["cpu_cores"] = int(m2.group(1))

        # Disk: "X GiB used of Y GiB (root)"
        m3 = re.search(
            r"(?:Disk|Storage)[^\n]*:\s*([\d.]+)\s*(GiB|GB|TiB|TB|MiB|MB)"
            r"\s+used\s+of\s+([\d.]+)\s*(GiB|GB|TiB|TB|MiB|MB)",
            text, re.I)
        if m3:
            out["disk_used_gb"]  = _to_gb(m3.group(1), m3.group(2))
            out["disk_total_gb"] = _to_gb(m3.group(3), m3.group(4))

        logger.debug("get_node_info(text) remote=%r → ram=%d/%dMB cpu=%d disk=%d/%dGB",
                     remote, out["ram_used_mb"], out["ram_total_mb"],
                     out["cpu_cores"], out["disk_used_gb"], out["disk_total_gb"])
        return out

    # ── Remotes ───────────────────────────────────────────────────────────────

    @classmethod
    def add_remote(cls, name: str, address: str, accept_cert: bool = True) -> dict:
        args = ["incus", "remote", "add", name, f"https://{address}:8443",
                "--auth-type", "tls"]
        if accept_cert:
            args.append("--accept-certificate")
        return cls._run(args, timeout=30)

    @classmethod
    def remove_remote(cls, name: str) -> dict:
        return cls._run(["incus", "remote", "remove", name])

    # ── Snapshots ─────────────────────────────────────────────────────────────

    @classmethod
    def create_snapshot(cls, name: str, snap: str, remote: Optional[str] = None) -> dict:
        return cls._run(["incus", "snapshot", "create", cls._target(name, remote), snap])

    @classmethod
    def restore_snapshot(cls, name: str, snap: str, remote: Optional[str] = None) -> dict:
        return cls._run(["incus", "snapshot", "restore", cls._target(name, remote), snap])

    @classmethod
    def delete_snapshot(cls, name: str, snap: str, remote: Optional[str] = None) -> dict:
        return cls._run(["incus", "snapshot", "delete", cls._target(name, remote), snap])

    @classmethod
    def list_snapshots(cls, name: str, remote: Optional[str] = None) -> list:
        r = cls._run(["incus", "snapshot", "list",
                      cls._target(name, remote), "--format", "json"])
        try:
            return json.loads(r["output"]) if r["success"] else []
        except json.JSONDecodeError:
            return []

    # ── Port Forwarding ───────────────────────────────────────────────────────

    @classmethod
    def add_port_forward(cls, name: str, proto: str, host_port: int,
                         container_port: int, remote: Optional[str] = None) -> dict:
        if proto not in ("tcp", "udp"):
            return {"success": False, "output": "", "error": "Invalid protocol", "returncode": 1}
        target = cls._target(name, remote)
        return cls._run([
            "incus", "config", "device", "add", target,
            f"port-{proto}-{host_port}", "proxy",
            f"listen={proto}:0.0.0.0:{host_port}",
            f"connect={proto}:127.0.0.1:{container_port}",
        ])

    @classmethod
    def remove_port_forward(cls, name: str, host_port: int, proto: str,
                            remote: Optional[str] = None) -> dict:
        target = cls._target(name, remote)
        return cls._run(["incus", "config", "device", "remove", target,
                         f"port-{proto}-{host_port}"])

    # ── Console PTY ───────────────────────────────────────────────────────────

    @classmethod
    async def open_pty(cls, name: str, remote: Optional[str] = None):
        target = f"{remote}:{name}" if remote else name
        return await asyncio.create_subprocess_exec(
            "incus", "exec", target, "--", "bash", "--login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def calculate_cost(ram: int, cpu: int, disk_gb: int) -> int:
        return (ram // 512) * 10 + cpu * 50 + disk_gb * 5

    @staticmethod
    def validate_name(name: str) -> bool:
        if not name or len(name) < 2 or len(name) > 40:
            return False
        return all(c.isalnum() or c == "-" for c in name) and not name.startswith("-")

    @staticmethod
    def is_safe_command(cmd: str) -> bool:
        lower = cmd.lower()
        return not any(b.lower() in lower for b in BLOCKED_PATTERNS)
